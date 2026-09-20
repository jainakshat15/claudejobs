# Deployment files

Keeping the queue running on the always-on machine.

| File | Platform | What it does |
| --- | --- | --- |
| `systemd/claudejobs-*.service` | Linux | One user unit per service, restarting on failure |
| `windows/Register-ClaudeJobsTasks.ps1` | Windows | Registers one Scheduled Task per service, started at logon |

## The one decision that matters

Each job opens its own terminal window, and a terminal window needs an
**interactive desktop session**. That rules out running the dispatcher as a
background service under a system account.

Pick one:

- **Terminal mode** (`TERMINAL_MODE=auto`, the default) — the machine stays
  logged in. On Windows, enable auto-login and use the Scheduled Tasks here. On
  Linux, run the units inside your desktop session.
- **Headless mode** (`TERMINAL_MODE=headless`) — no windows at all; Claude's
  output goes to `LOG_DIR/job-<id>.out.log`. This is the right choice for a
  server, a VM without a desktop, or anything you want to run as a true service.

Nothing else changes between the two: the same jobs, the same bots, the same
logs. You can switch at any time by editing `.env` and restarting the dispatcher.

## Linux

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
# edit WorkingDirectory/ExecStart if the repo is not at ~/claudejobs
systemctl --user daemon-reload
systemctl --user enable --now claudejobs-api claudejobs-dispatcher claudejobs-telegram

# keep the units running when nobody is logged in
sudo loginctl enable-linger "$USER"

systemctl --user status claudejobs-dispatcher
journalctl --user -u claudejobs-dispatcher -f
```

## Windows

```powershell
cd D:\claudejobs
.\deploy\windows\Register-ClaudeJobsTasks.ps1

Start-ScheduledTask -TaskName claudejobs-api
Get-ScheduledTask -TaskName claudejobs-*

# to undo
.\deploy\windows\Register-ClaudeJobsTasks.ps1 -Remove
```

The script registers only the bots whose tokens are filled in, and points each
task at `.venv\Scripts\python.exe` in the repo.

For day-to-day running, checks and troubleshooting, see
[`../docs/OPERATIONS.md`](../docs/OPERATIONS.md).
