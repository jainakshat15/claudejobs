## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How it was checked

<!-- Which tests you ran, and on which OS. Say plainly if something is untested. -->

- [ ] `pytest` passes (unit tests)
- [ ] `pytest` passes with `CLAUDEJOBS_TEST_DATABASE_URL` set (database and API tests)
- [ ] `ruff check claudejobs tests` is clean
- [ ] Tested on: <!-- Windows / Linux / macOS -->

## Checklist

- [ ] New or changed behaviour has a test
- [ ] Schema changes are a **new** migration file, not an edit to an applied one
- [ ] New settings are in `.env.example` with a comment
- [ ] Route changes are reflected in `docs/API.md`
- [ ] Anything touching tokens, allowlists or permissions says so below

## Security impact

<!-- "None" is a fine answer. If it touches auth, allowlists, ALLOWED_ROOTS, or
     what a job may execute, describe what changes for someone running this. -->
