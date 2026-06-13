# tgbridge/panel.py — mode management, keyboards, and live status panel.
# Depends on: config, tgapi, state.
import os
import sys
import time

from tgbridge.config import (
    MODE_FILE, PANEL_MSG, CHAT, CONTEXTS, _PANEL_DEBOUNCE,
    MODES, MODE_ORDER, DEFAULT_MODE, PENDING, ASK_PEND,
)
from tgbridge.tgapi import api_json
from tgbridge.state import lock, running, queues

# Debounce: timestamp of last panel edit
_panel_last_edit: float = 0.0


# ---------------------------------------------------------------------------
# Mode management
# ---------------------------------------------------------------------------

def get_mode():
    try:
        m = open(MODE_FILE).read().strip()
        return m if m in CONTEXTS else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def set_mode(m):
    try:
        open(MODE_FILE, "w").write(m)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def mode_keyboard():
    """Inline keyboard with mode selectors (for the live panel). Layout 4+3."""
    buttons = [
        {"text": "%s %s" % (MODES[k]["emoji"], k), "callback_data": "mode:%s" % k}
        for k in MODE_ORDER
    ]
    return {"inline_keyboard": [buttons[:4], buttons[4:]]}


def persistent_reply_keyboard():
    """Persistent reply keyboard — always visible in chat. Layout 4+3 + Панель/Help."""
    mode_buttons_row1 = [{"text": "%s %s" % (MODES[k]["emoji"], k)} for k in MODE_ORDER[:4]]
    mode_buttons_row2 = [{"text": "%s %s" % (MODES[k]["emoji"], k)} for k in MODE_ORDER[4:]]
    return {
        "keyboard": [
            mode_buttons_row1,
            mode_buttons_row2,
            [{"text": "📟 Панель"}, {"text": "❓ Help"}],
        ],
        "is_persistent": True,
        "resize_keyboard": True,
    }


# ---------------------------------------------------------------------------
# Live panel
# ---------------------------------------------------------------------------

def _panel_text() -> str:
    """Render panel text from current mode and task registry state."""
    mode = get_mode()
    with lock:
        run_keys = list(running.keys())
        total_queued = sum(len(q) for q in queues.values())
    if os.path.exists(PENDING):
        status_str = "🟡 Ожидаю подтверждения"
    elif os.path.exists(ASK_PEND):
        status_str = "🟡 Ожидаю ответа"
    elif run_keys:
        label = run_keys[0]
        queue_str = (" +%d в очереди" % total_queued) if total_queued else ""
        status_str = "🔴 Работает [%s]%s" % (label, queue_str)
    else:
        status_str = "🟢 Свободен"
    return "Режим: %s | Статус: %s" % (mode, status_str)


def _read_panel_id() -> "int | None":
    """Read saved panel message_id from disk."""
    try:
        return int(open(PANEL_MSG).read().strip())
    except Exception:
        return None


def _save_panel_id(msg_id: int) -> None:
    try:
        open(PANEL_MSG, "w").write(str(msg_id))
    except Exception:
        pass


def refresh_panel() -> None:
    """Edit (or create) the live status panel message. Debounced, idempotent."""
    global _panel_last_edit
    now = time.monotonic()
    if now - _panel_last_edit < _PANEL_DEBOUNCE:
        return
    _panel_last_edit = now

    text = _panel_text()
    kb = mode_keyboard()
    panel_id = _read_panel_id()

    if panel_id is not None:
        try:
            api_json("editMessageText", {
                "chat_id": CHAT,
                "message_id": panel_id,
                "text": text,
                "reply_markup": kb,
            })
            return
        except Exception as e:
            err_str = str(e)
            if "message is not modified" in err_str:
                return
            sys.stderr.write("panel edit error (will resend): %s\n" % e)

    try:
        resp = api_json("sendMessage", {
            "chat_id": CHAT,
            "text": text,
            "reply_markup": kb,
        })
        new_id = resp["result"]["message_id"]
        _save_panel_id(new_id)
    except Exception as e:
        sys.stderr.write("panel send error: %s\n" % e)
