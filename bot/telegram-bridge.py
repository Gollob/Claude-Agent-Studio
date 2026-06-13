#!/usr/bin/env python3
# telegram-bridge.py — thin entry point for agent-vm Telegram-Claude bridge.
#
# Loads tgbridge package from RUNTIME_DIR (TG_BRIDGE_RUNTIME env var, default
# ~/agent/tg-bot-runtime) or from the directory containing this file (dev/test).
# Then imports main() from tgbridge.app and re-exports public symbols so that
# flat access _bridge.<name> in tests resolves to the canonical module objects.
#
# Patching strategy (ADR-001 compliant):
#   Tests that monkeypatch symbols via _bridge.X are patching the entry module's
#   namespace.  For that patch to be visible on the real execution path, all
#   cross-module calls in workers.py / handlers.py use module-qualified
#   references (import tgbridge.state as _state; _state.finish(...)).
#   Therefore patching tgbridge.state.finish (or equivalently _bridge.finish
#   after the re-import below) IS visible to _claude_worker at call time.
#
# Re-exports: every name tested via _bridge.X is imported here from its
# defining module so that monkeypatch.setattr(_bridge, X, mock) updates the
# same object the runtime calls through.
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path setup — must happen before any tgbridge import
# ---------------------------------------------------------------------------

RUNTIME_DIR: str = os.environ.get(
    "TG_BRIDGE_RUNTIME",
    os.path.expanduser("~/agent/tg-bot-runtime"),
)
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)

# Also add the directory containing this file (repo root in dev/test).
_REPO_DIR: str = str(Path(__file__).parent.resolve())
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

# ---------------------------------------------------------------------------
# Package import
# ---------------------------------------------------------------------------

from tgbridge.app import main  # noqa: E402

# ---------------------------------------------------------------------------
# Re-exports — flat access for tests (_bridge.<name>) and legacy callers.
# Each name is imported from its defining module so that a monkeypatch on
# this entry module's attribute AND on the defining module attribute are
# equivalent (both reach the same runtime call path via module-qualified refs).
# ---------------------------------------------------------------------------

# config constants
from tgbridge.config import (  # noqa: F401
    TOKEN, API, CHAT,
    IPC, PENDING, DECISION, ASK_PEND, ASK_ANS, MODE_FILE, PANEL_MSG,
    FILE_INTAKE_URL,
    CONTEXTS, SHORTCUTS,
    YES, NO, HELP,
    _CHILD_ENV_ALLOWLIST, _CHILD_ENV_ALLOWLIST_PREFIXES, _CHILD_ENV_DENYLIST,
    _KILL_GRACE, _PANEL_DEBOUNCE,
)

# secrets
from tgbridge.secrets import child_env, mask_secrets  # noqa: F401

# process
from tgbridge.process import _kill_tree  # noqa: F401

# state registry — tests access lock/running/queues/tasks_by_id/enqueue/finish/Task
from tgbridge.state import (  # noqa: F401
    Task, lock, running, queues, tasks_by_id,
    _new_task_id, enqueue, finish,
)

# tgapi
from tgbridge.tgapi import api, api_json, send  # noqa: F401

# panel
from tgbridge.panel import (  # noqa: F401
    get_mode, set_mode, refresh_panel,
    mode_keyboard, persistent_reply_keyboard,
)

# claude runner
from tgbridge.claude_runner import run_claude  # noqa: F401

# workers
from tgbridge.workers import (  # noqa: F401
    _claude_worker, _start_worker_thread, dispatch,
)

# handlers
from tgbridge.handlers import (  # noqa: F401
    _cancel_task, _cancel_key, _cancel_queued_by_id,
    handle_cancel, handle_queue, handle_text, handle_approval_callback,
    _REPLY_BUTTON_MAP,
)

if __name__ == "__main__":
    main()
