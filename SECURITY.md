# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `main` branch | Yes |
| Older tagged releases | Best effort |

---

## Reporting a vulnerability

If you discover a security vulnerability, **please do not open a public GitHub issue.**

Instead, report it privately:

- **Email:** security@your-org.example  *(replace with your actual contact)*
- **Subject line:** `[claude-agent-studio] Security vulnerability report`

Include:
- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof-of-concept (if safe to share).
- The component affected (`bot/`, `services/task-router/`, `services/file-intake/`, or `agents/`).

We will acknowledge receipt within 3 business days and aim to provide a fix or mitigation
within 14 days for critical issues.

---

## Security design principles

### Secrets are environment-variable only

No secrets, API keys, tokens, or passwords are stored in source code or committed to the
repository. All sensitive values are loaded exclusively from environment variables at runtime.

The `.env.example` file contains only placeholder values (empty strings). The actual `.env`
file is listed in `.gitignore` and must never be committed.

**Before every commit, run:**
```bash
make scan
```

This runs `gitleaks` plus a targeted `grep` for common secret patterns (API key prefixes,
numeric chat IDs). A clean scan is required for all PRs.

### Telegram token isolation

`TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are explicitly excluded from the child process
environment when launching Claude agents (`_CHILD_ENV_DENYLIST` in `bot/tgbridge/config.py`).
Agent subprocesses cannot access the bot credentials.

### Approval gate for shell commands

All shell commands issued by Claude agents pass through `bot/approval-hook.sh` (a Claude
PreToolUse hook). The classifier (`bot/classifier.py`) uses a deny-first policy:

1. **Bypass patterns** — eval/pipe tricks, base64-decoded execution → immediate deny.
2. **Denylist** — destructive commands by name and flag → immediate deny.
3. **Allowlist** — known safe read-only operations → allow.
4. **Unknown** → send inline approval buttons to Telegram; block until the user responds
   (timeout 300 s → automatic deny).

This means no destructive command runs silently, even if Claude generates one.

### Network exposure

Services bind to `127.0.0.1` by default:
- `task-router`: `127.0.0.1:8092`
- `file-intake`: `127.0.0.1:8090`

The Whisper STT container is not exposed on any host port. The bot communicates internally
via Docker's service network. Do not expose these ports directly to the internet without
adding authentication.

### File upload limits

`file-intake` enforces a configurable maximum upload size (`MAX_FILE_MB`, default 50 MB)
and validates content types before processing.

### No credentials in logs

The bot masks secrets in log output (`bot/tgbridge/secrets.py`). Do not add logging
statements that print raw environment variable values.

---

## Known limitations

- The approval gate operates at the PreToolUse hook level and applies to commands issued
  inside a Claude session. Commands run outside of Claude sessions (e.g., direct shell access
  to the host) are not governed by this gate.
- The bot is designed for single-user (personal) deployments. It does not implement
  multi-user access control; `TELEGRAM_CHAT_ID` gates all inbound messages to one chat.

---

## Dependency updates

We recommend enabling GitHub's Dependabot or Renovate for automatic dependency pull requests.
Review dependency updates before merging, particularly for `services/file-intake/app/requirements.txt`
and `services/task-router/go.mod`.
