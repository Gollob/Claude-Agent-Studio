# tgbridge/config.py — environment, paths, and constants.
# All other modules import from here; this module depends only on stdlib.
import os

# ---------------------------------------------------------------------------
# Telegram credentials and API base
# ---------------------------------------------------------------------------

# TOKEN and CHAT are required env vars. Bot will print a hint and exit gracefully
# if TELEGRAM_TOKEN is missing (useful for CI/demo without a real bot).
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT  = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
API   = "https://api.telegram.org/bot" + TOKEN

# ---------------------------------------------------------------------------
# IPC paths
# ---------------------------------------------------------------------------

IPC       = os.environ.get("AGENT_IPC_DIR", "/tmp/agent-studio")
PENDING   = IPC + "/approval.pending"
DECISION  = IPC + "/approval.decision"
ASK_PEND  = IPC + "/ask.pending"
ASK_ANS   = IPC + "/ask.answer"
MODE_FILE = IPC + "/mode"
PANEL_MSG = IPC + "/panel.msg"          # stores panel message_id (int as text)

FILE_INTAKE_URL = os.getenv("FILE_INTAKE_URL", "http://localhost:8090")

os.makedirs(IPC, exist_ok=True)

# ---------------------------------------------------------------------------
# Agent workdir base (configure via AGENT_WORKDIR env)
# ---------------------------------------------------------------------------

AGENT_WORKDIR = os.environ.get("AGENT_WORKDIR", os.path.expanduser("~/agents"))

# ---------------------------------------------------------------------------
# Mode registry — single source of truth
#
# Customize modes via AGENT_WORKDIR; add or remove entries to match your
# agent layout. Paths below are relative to AGENT_WORKDIR.
# ---------------------------------------------------------------------------

MODES = {
    "ask": {"path": AGENT_WORKDIR,                             "emoji": "🤝", "desc": "General assistant"},
    "dev": {"path": os.path.join(AGENT_WORKDIR, "dev"),        "emoji": "💻", "desc": "Dev studio"},
}
MODE_ORDER   = list(MODES.keys())
DEFAULT_MODE = "ask"

# Derived — backward-compatible alias (all other modules import CONTEXTS)
CONTEXTS = {k: v["path"] for k, v in MODES.items()}

# ---------------------------------------------------------------------------
# Mode contexts and command shortcuts
# ---------------------------------------------------------------------------

SHORTCUTS = {
    "go": "go-dev", "py": "python-dev", "ts": "ts-dev", "rev": "reviewer",
    "devops": "devops", "db": "db-engineer", "docs": "docs", "qa": "qa-test",
}

def _modes_help_lines() -> str:
    return " · ".join(
        "/%s %s" % (k, MODES[k]["desc"])
        for k in MODE_ORDER
    )


def _modes_oneliner() -> str:
    """Generate 'One-shot' line from MODE_ORDER (keeps in sync automatically)."""
    parts = [("/%s <text>" % k) for k in MODE_ORDER]
    return ", ".join(parts)


HELP = (
    "Claude Agent Studio bot online — managing AI agent team.\n"
    "Modes (sticky): " + _modes_help_lines() + "\n"
    "One-shot: " + _modes_oneliner() + "\n"
    "Specialists: /go /py /ts /rev /devops /db /docs /qa <text>\n"
    "Mode menu: /mode · /status — status panel\n"
    "/queue — task queue · /cancel [key|all] — cancel\n"
    "Voice → command in current mode · File/photo → AI processing\n"
    "Approval for dangerous commands: buttons or YES/NO text."
)

# YES/NO sets for text fallback approval
YES = {"yes", "y", "ok", "approve", "+", "да", "ок", "разрешаю"}
NO  = {"no", "n", "deny", "cancel", "-", "нет", "отмена", "запрещаю"}

# ---------------------------------------------------------------------------
# Child process env allowlist/denylist
# ---------------------------------------------------------------------------

_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "LOGNAME",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR", "CLAUDE_HOME",
    "NODE_PATH", "NPM_CONFIG_PREFIX",
    "UV_CACHE_DIR", "UV_PYTHON", "VIRTUAL_ENV", "PYTHONPATH",
    "CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD",
    "TRILIUM_ETAPI_TOKEN", "TRILIUM_URL", "AGENT_COMMS_URL",
    "AGENT_WORKDIR", "AGENT_IPC_DIR",
    "TMPDIR", "TMP", "TEMP",
    "DBUS_SESSION_BUS_ADDRESS",
})
_CHILD_ENV_ALLOWLIST_PREFIXES = ("LC_", "XDG_")
_CHILD_ENV_DENYLIST = frozenset({"TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"})

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

_KILL_GRACE    = 5    # seconds between SIGTERM and SIGKILL
_PANEL_DEBOUNCE = 1.0  # minimum seconds between panel edits
