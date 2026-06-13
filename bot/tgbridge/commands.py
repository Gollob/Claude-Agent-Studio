# tgbridge/commands.py — bot command registration and menu.
# Depends on: config, tgapi, panel.
import sys

from tgbridge.config import CHAT, MODES, MODE_ORDER
from tgbridge.tgapi import api_json, send
from tgbridge.panel import get_mode, mode_keyboard, refresh_panel


def setup_bot_commands():
    """Register /commands list and menu button with Telegram."""
    # Mode commands — generated from MODES/MODE_ORDER (single source of truth)
    mode_cmds = [
        {"command": k, "description": "%s %s" % (MODES[k]["emoji"], MODES[k]["desc"])}
        for k in MODE_ORDER
    ]
    commands = mode_cmds + [
        # System commands
        {"command": "mode",   "description": "Switch mode (menu)"},
        {"command": "status", "description": "Show status panel"},
        {"command": "queue",  "description": "Task queue"},
        {"command": "cancel", "description": "Cancel task: /cancel [key|all]"},
        {"command": "help",   "description": "Help"},
        {"command": "start",  "description": "Start"},
        # Specialist shortcuts
        {"command": "go",     "description": "Agent go-dev"},
        {"command": "py",     "description": "Agent python-dev"},
        {"command": "ts",     "description": "Agent ts-dev"},
        {"command": "rev",    "description": "Agent reviewer"},
        {"command": "devops", "description": "Agent devops"},
        {"command": "db",     "description": "Agent db-engineer"},
        {"command": "docs",   "description": "Agent docs"},
        {"command": "qa",     "description": "Agent qa-test"},
    ]
    try:
        api_json("setMyCommands", {"commands": commands})
    except Exception as e:
        sys.stderr.write("setMyCommands error: %s\n" % e)
    try:
        api_json("setChatMenuButton", {
            "chat_id": CHAT,
            "menu_button": {"type": "commands"},
        })
    except Exception as e:
        sys.stderr.write("setChatMenuButton error: %s\n" % e)


def show_menu(reply_to=None):
    """Show mode selection menu (legacy inline-keyboard). Also refresh panel."""
    refresh_panel()
    send("Current mode: %s. Choose context:" % get_mode(), reply_to, mode_keyboard())
