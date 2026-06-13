# tg-bot — Telegram bridge for Claude Agent Studio

Telegram bot for controlling Claude agents via a personal chat.
Routes messages by mode (dev/ask/...), persistent keyboard, live status panel,
button-based approval gate for dangerous commands, deny-first command classifier.

## Files

| File | Purpose |
|---|---|
| `telegram-bridge.py` | Main bot process (long-poll, routing, media, IPC, panel, approval) |
| `approval-hook.sh` | PreToolUse hook for Claude: classifies command, sends Telegram approval buttons on `ask` |
| `classifier.py` | Command classifier allow/ask/deny (deny-first, per-segment, 116 tests) |
| `deploy.sh` | Deploy script (idempotent, --dry-run, --rollback) |
| `.env.example` | Example environment variables |
| `tests/` | pytest tests |

## Secrets

Secrets (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) are stored in `~/.config/telegram-bridge.env`
(chmod 600). Never committed (`.gitignore` covers `*.env`).

For systemd deployment, the unit file loads variables from that file.

## Deploy (deploy.sh)

The script is idempotent. For Docker-based deployment see the top-level `docker-compose.yml`.

For systemd deployment, two NOPASSWD sudo rules are used:
1. Copy the bridge script to the runtime location
2. `sudo systemctl restart|start|stop|status telegram-bridge`

Any other sudo requires a password and will fail explicitly.

### Deploy commands

```bash
# Preview changes (dry run):
bash deploy.sh --dry-run

# Deploy:
bash deploy.sh

# Roll back from .bak backups:
bash deploy.sh --rollback
```

### Rollback

`--rollback` restores `.bak` files:
- `telegram-bridge.py` staging copy
- `approval-hook.sh`
- `classifier.py`
- `~/.claude/settings.json`

Then restarts the bridge service.

## Menu and panel (ADR-002)

`setMyCommands` + `setChatMenuButton` (static command list) + persistent reply keyboard
+ live panel (single inline message, edited in place: `Mode: X | Status: free / running [label]`).
Panel message id stored in `/tmp/agent/panel.msg`.

## Button approval gate (ADR-003)

Hook classifies the command. On `ask` verdict — writes PENDING JSON
`/tmp/agent/approval.pending` `{id,cmd,category,reason,ts}` and sends a message with
`Allow` / `Deny` buttons (callback `approve:allow|deny:<id>`). Bridge checks `id`,
writes DECISION `/tmp/agent/approval.decision`, edits the message. Timeout 300 s → block.
Text yes/no kept as fallback.

## Classifier (ADR-004)

`classifier.py` — pure function `classify(cmd) -> {verdict, category, reason}`.
Deny-first: bypass checks (eval/curl|sh/base64), then denylist by command name and flags,
then read-only allowlist, then UNKNOWN_POLICY=ask. Each pipeline segment / `;` / `&&` is
checked independently. Allowlist/denylist are data structures at the top of the file,
editable without rewriting logic.

The hook applies to ALL Claude Bash sessions on the machine. To allow noisy routine commands —
add them to ALLOWLIST in `classifier.py` and redeploy. The hook is picked up at session START.

## Dependencies

- Python 3 stdlib (no pip dependencies in the core bot)
- `curl` — used in approval-hook.sh
- `claude` CLI — for launching agents
