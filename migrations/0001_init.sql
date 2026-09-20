-- 0001_init.sql — job queue schema for the Claude job runner.
--
-- Applied by: python -m claudejobs migrate up
-- Every statement is idempotent where practical so a half-applied migration can
-- be re-run safely. Requires PostgreSQL 13+ (gen_random_uuid is built in).

-- ---------------------------------------------------------------------------
-- updated_at trigger helper
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- jobs — the single job + worker table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    -- identity ---------------------------------------------------------------
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    title               text,

    -- payload: everything the worker needs to start Claude -------------------
    prompt              text NOT NULL CHECK (length(btrim(prompt)) > 0),
    directory           text NOT NULL CHECK (length(btrim(directory)) > 0),
    model               text,
    permission_mode     text,
    append_system_prompt text,
    payload             jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- queue state ------------------------------------------------------------
    status              text NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'waiting_input',
                                          'succeeded', 'failed', 'cancelled', 'timed_out')),
    priority            integer NOT NULL DEFAULT 100,
    attempts            integer NOT NULL DEFAULT 0,
    max_attempts        integer NOT NULL DEFAULT 1 CHECK (max_attempts >= 1),
    timeout_minutes     integer CHECK (timeout_minutes IS NULL OR timeout_minutes > 0),
    scheduled_at        timestamptz NOT NULL DEFAULT now(),

    -- worker state -----------------------------------------------------------
    worker_id           text,
    worker_host         text,
    worker_pid          integer,
    session_id          uuid,
    job_token_hash      text,
    log_path            text,
    last_heartbeat_at   timestamptz,
    lease_expires_at    timestamptz,

    -- where the job came from, so Claude can reach the poster back -----------
    source              text NOT NULL DEFAULT 'http'
                        CHECK (source IN ('http', 'cli', 'telegram', 'slack')),
    source_user_id      text,
    source_username     text,
    source_chat_id      text,
    source_thread_id    text,
    source_message_id   text,

    -- control ----------------------------------------------------------------
    cancel_requested    boolean NOT NULL DEFAULT false,
    cancel_reason       text,

    -- outcome ----------------------------------------------------------------
    result_summary      text,
    error               text,
    exit_code           integer,

    -- bookkeeping ------------------------------------------------------------
    created_by          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    queued_at           timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,

    CONSTRAINT jobs_public_id_key UNIQUE (public_id)
);

-- The queue lookup: claimable jobs only, in dispatch order.
CREATE INDEX IF NOT EXISTS jobs_queue_idx
    ON jobs (priority, scheduled_at, id)
    WHERE status = 'queued' AND cancel_requested = false;

-- Active jobs, used for capacity checks and lease expiry sweeps.
CREATE INDEX IF NOT EXISTS jobs_active_idx
    ON jobs (worker_id, status)
    WHERE status IN ('running', 'waiting_input');

CREATE INDEX IF NOT EXISTS jobs_lease_idx
    ON jobs (lease_expires_at)
    WHERE status IN ('running', 'waiting_input');

-- "show me my jobs" from a bot.
CREATE INDEX IF NOT EXISTS jobs_source_user_idx
    ON jobs (source, source_user_id, id DESC);

CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs (status, id DESC);

DROP TRIGGER IF EXISTS jobs_set_updated_at ON jobs;
CREATE TRIGGER jobs_set_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- job_events — append-only audit trail of everything that happened to a job
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      bigint NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind        text NOT NULL,
    from_status text,
    to_status   text,
    detail      text,
    actor       text,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id, id DESC);


-- ---------------------------------------------------------------------------
-- job_messages — the two-way channel between Claude and the job's poster
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_messages (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      bigint NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('question', 'answer', 'note')),
    body        text NOT NULL,
    author      text,
    parent_id   bigint REFERENCES job_messages (id) ON DELETE CASCADE,
    -- questions only: open -> answered | expired | cancelled
    status      text CHECK (status IN ('open', 'answered', 'expired', 'cancelled')),
    answered_at timestamptz,
    answered_by text,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT job_messages_question_status
        CHECK ((kind = 'question') = (status IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS job_messages_job_idx ON job_messages (job_id, id);

-- At most one unanswered question per job, and a fast lookup for the sweeper.
CREATE UNIQUE INDEX IF NOT EXISTS job_messages_one_open_question_idx
    ON job_messages (job_id)
    WHERE kind = 'question' AND status = 'open';

CREATE INDEX IF NOT EXISTS job_messages_open_questions_idx
    ON job_messages (created_at)
    WHERE kind = 'question' AND status = 'open';


-- ---------------------------------------------------------------------------
-- outbound_messages — queue of things the bots must deliver to humans
--
-- The API never talks to Telegram/Slack directly. It enqueues here; whichever
-- bot process owns the channel claims rows (FOR UPDATE SKIP LOCKED), delivers
-- them, and writes back the provider message id. That id is what lets a plain
-- "reply to this message" find its way back to the right job.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbound_messages (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id              bigint REFERENCES jobs (id) ON DELETE CASCADE,
    question_id         bigint REFERENCES job_messages (id) ON DELETE SET NULL,
    channel             text NOT NULL CHECK (channel IN ('telegram', 'slack')),
    chat_id             text NOT NULL,
    thread_id           text,
    reply_to_message_id text,
    kind                text NOT NULL DEFAULT 'notice',
    body                text NOT NULL,
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'claimed', 'sent', 'failed')),
    attempts            integer NOT NULL DEFAULT 0,
    max_attempts        integer NOT NULL DEFAULT 5,
    last_error          text,
    claimed_by          text,
    claimed_at          timestamptz,
    sent_at             timestamptz,
    provider_message_id text,
    provider_thread_id  text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS outbound_pending_idx
    ON outbound_messages (channel, id)
    WHERE status IN ('pending', 'claimed');

-- Reply routing: "this incoming reply points at provider message X".
CREATE INDEX IF NOT EXISTS outbound_provider_msg_idx
    ON outbound_messages (channel, chat_id, provider_message_id);

CREATE INDEX IF NOT EXISTS outbound_thread_idx
    ON outbound_messages (channel, chat_id, provider_thread_id);


-- ---------------------------------------------------------------------------
-- Convenience view for dashboards and the /stats endpoint
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW job_queue_stats AS
SELECT status,
       count(*)                                                   AS jobs,
       min(created_at)                                            AS oldest_created_at,
       avg(EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)))
                                                                  AS avg_runtime_seconds
FROM jobs
GROUP BY status;
