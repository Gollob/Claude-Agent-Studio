"""Tests for general-conductor changes (T1–T6, T10, T11).

Key invariant: CONTEXTS keys, mode_keyboard() mode-keys,
persistent_reply_keyboard() mode-keys, and _REPLY_BUTTON_MAP mode-keys
are all equal to set(MODES).

Covers:
  - T1:  MODES registry has keys; CONTEXTS == {k: v["path"] for k,v in MODES};
         DEFAULT_MODE == "ask".
  - T2:  get_mode() fallback on invalid/empty file → DEFAULT_MODE ("ask").
  - T3:  mode_keyboard() and persistent_reply_keyboard() cover all modes;
         persistent keyboard preserves Panel/Help row.
  - T4:  _REPLY_BUTTON_MAP mode-keys == set(MODES); Panel/Help preserved.
  - T5:  setup_bot_commands() command list contains all mode commands
         and all specialist shortcuts (no regression).
  - T6:  Invariant test — all four surfaces share the same key set.
  - T10: _done_ping sends completion message with elapsed time; suppressed on cancelling;
         includes next-task hint when queued.
  - T11: _panel_text() with PENDING/ASK_PEND files present shows waiting state.
"""

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path and dummy env vars are set.
# conftest.py pre-loads tgbridge.config from the dev tree at session start,
# so by the time this file is imported, sys.modules already has the dev
# versions of tgbridge.*.  Direct imports here are therefore safe.
# ---------------------------------------------------------------------------

REPO_DIR = Path(__file__).parent.parent.resolve()
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

for _k, _v in [("TELEGRAM_TOKEN", "DUMMY_CONDUCTOR_TOKEN"), ("TELEGRAM_CHAT_ID", "0")]:
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# Module imports
# ---------------------------------------------------------------------------

from tgbridge.config import (
    MODES, CONTEXTS, MODE_ORDER, DEFAULT_MODE, PENDING, ASK_PEND,
)
import tgbridge.panel as _panel_mod
import tgbridge.handlers as _handlers_mod
import tgbridge.workers as _workers_mod
import tgbridge.tgapi as _tgapi_mod
import tgbridge.state as _state_mod


# ============================================================================
# T1 — MODES registry structure
# ============================================================================

class TestModesRegistry:

    def test_modes_has_at_least_one_key(self):
        assert len(MODES) >= 1

    def test_modes_contains_ask(self):
        """'ask' (general assistant) must always be present."""
        assert "ask" in MODES

    def test_modes_contains_dev(self):
        """'dev' (dev studio) must be present."""
        assert "dev" in MODES

    def test_contexts_derived_from_modes(self):
        expected = {k: v["path"] for k, v in MODES.items()}
        assert CONTEXTS == expected

    def test_default_mode_is_ask(self):
        assert DEFAULT_MODE == "ask"

    def test_mode_order_covers_all_modes(self):
        assert set(MODE_ORDER) == set(MODES)
        assert len(MODE_ORDER) == len(MODES)

    def test_ask_path_is_agent_workdir(self):
        """ask mode path should be the root AGENT_WORKDIR."""
        from tgbridge.config import AGENT_WORKDIR
        assert MODES["ask"]["path"] == AGENT_WORKDIR

    def test_each_mode_has_required_keys(self):
        for key, mode in MODES.items():
            assert "path" in mode, "mode %r missing 'path'" % key
            assert "emoji" in mode, "mode %r missing 'emoji'" % key
            assert "desc" in mode, "mode %r missing 'desc'" % key


# ============================================================================
# T2 — get_mode() fallback
# ============================================================================

class TestGetMode:

    def test_fallback_on_missing_file(self, tmp_path):
        """Non-existent MODE_FILE → DEFAULT_MODE."""
        fake_path = str(tmp_path / "no_such_file")
        with patch.object(_panel_mod, "MODE_FILE", fake_path):
            result = _panel_mod.get_mode()
        assert result == DEFAULT_MODE

    def test_fallback_on_empty_file(self, tmp_path):
        """Empty MODE_FILE → DEFAULT_MODE."""
        mode_file = tmp_path / "mode"
        mode_file.write_text("")
        with patch.object(_panel_mod, "MODE_FILE", str(mode_file)):
            result = _panel_mod.get_mode()
        assert result == DEFAULT_MODE

    def test_fallback_on_invalid_mode(self, tmp_path):
        """Unknown value in MODE_FILE → DEFAULT_MODE."""
        mode_file = tmp_path / "mode"
        mode_file.write_text("invalid_mode_xyz")
        with patch.object(_panel_mod, "MODE_FILE", str(mode_file)):
            result = _panel_mod.get_mode()
        assert result == DEFAULT_MODE

    def test_valid_mode_returned(self, tmp_path):
        """Valid mode stored in MODE_FILE → returned as-is."""
        mode_file = tmp_path / "mode"
        mode_file.write_text("dev")
        with patch.object(_panel_mod, "MODE_FILE", str(mode_file)):
            result = _panel_mod.get_mode()
        assert result == "dev"

    def test_default_is_ask(self, tmp_path):
        """Regression: fallback is 'ask'."""
        fake_path = str(tmp_path / "no_file")
        with patch.object(_panel_mod, "MODE_FILE", fake_path):
            result = _panel_mod.get_mode()
        assert result == "ask"


# ============================================================================
# T3 — keyboards contain all modes
# ============================================================================

class TestKeyboards:

    def _mode_keys_from_inline(self):
        """Extract mode keys from mode_keyboard() inline buttons."""
        kb = _panel_mod.mode_keyboard()
        keys = set()
        for row in kb["inline_keyboard"]:
            for btn in row:
                data = btn["callback_data"]
                if data.startswith("mode:"):
                    keys.add(data[len("mode:"):])
        return keys

    def _mode_texts_from_reply(self):
        """Extract mode button texts from persistent_reply_keyboard()."""
        kb = _panel_mod.persistent_reply_keyboard()
        texts = set()
        for row in kb["keyboard"]:
            for btn in row:
                text = btn["text"]
                for k, m in MODES.items():
                    if text == "%s %s" % (m["emoji"], k):
                        texts.add(k)
                        break
        return texts

    def test_mode_keyboard_covers_all_modes(self):
        keys = self._mode_keys_from_inline()
        assert keys == set(MODES), (
            "mode_keyboard() mode-keys %r != set(MODES) %r" % (keys, set(MODES))
        )

    def test_persistent_reply_keyboard_covers_all_modes(self):
        keys = self._mode_texts_from_reply()
        assert keys == set(MODES), (
            "persistent_reply_keyboard() mode-keys %r != set(MODES) %r" % (keys, set(MODES))
        )

    def test_persistent_reply_keyboard_has_panel_and_help(self):
        kb = _panel_mod.persistent_reply_keyboard()
        all_texts = {btn["text"] for row in kb["keyboard"] for btn in row}
        assert "📟 Панель" in all_texts, "Panel button missing from persistent keyboard"
        assert "❓ Help" in all_texts, "Help button missing from persistent keyboard"


# ============================================================================
# T4 — _REPLY_BUTTON_MAP
# ============================================================================

class TestReplyButtonMap:

    def _mode_keys_from_map(self):
        """Extract mode keys from _REPLY_BUTTON_MAP (exclude Panel/Help)."""
        keys = set()
        for text, cmd in _handlers_mod._REPLY_BUTTON_MAP.items():
            if cmd not in ("/status", "/help"):
                keys.add(cmd.lstrip("/"))
        return keys

    def test_reply_button_map_mode_keys_equal_modes(self):
        keys = self._mode_keys_from_map()
        assert keys == set(MODES), (
            "_REPLY_BUTTON_MAP mode-keys %r != set(MODES) %r" % (keys, set(MODES))
        )

    def test_reply_button_map_has_panel_and_help(self):
        assert "📟 Панель" in _handlers_mod._REPLY_BUTTON_MAP
        assert "❓ Help" in _handlers_mod._REPLY_BUTTON_MAP
        assert _handlers_mod._REPLY_BUTTON_MAP["📟 Панель"] == "/status"
        assert _handlers_mod._REPLY_BUTTON_MAP["❓ Help"] == "/help"

    def test_reply_button_map_text_format(self):
        """Each mode entry has the form '{emoji} {key}' → '/{key}'."""
        for k, m in MODES.items():
            expected_text = "%s %s" % (m["emoji"], k)
            expected_cmd = "/%s" % k
            assert expected_text in _handlers_mod._REPLY_BUTTON_MAP, (
                "Missing key %r in _REPLY_BUTTON_MAP" % expected_text
            )
            assert _handlers_mod._REPLY_BUTTON_MAP[expected_text] == expected_cmd


# ============================================================================
# T5 — setup_bot_commands() no regression
# ============================================================================

class TestSetupBotCommands:

    def _capture_commands(self):
        """Call setup_bot_commands with mocked API; return command list."""
        captured = []

        def fake_api_json(method, payload, timeout=30):
            if method == "setMyCommands":
                captured.append(payload["commands"])
            return {"ok": True, "result": {}}

        import tgbridge.commands as _cmd_mod

        with patch.object(_cmd_mod, "api_json", fake_api_json):
            _cmd_mod.setup_bot_commands()

        return captured[0] if captured else []

    def test_all_mode_commands_present(self):
        cmds = self._capture_commands()
        cmd_names = {c["command"] for c in cmds}
        for key in MODES:
            assert key in cmd_names, "/%s missing from setMyCommands" % key

    def test_specialist_shortcuts_not_removed(self):
        """Regression: /go /py /ts /rev /devops /db /docs /qa must remain."""
        cmds = self._capture_commands()
        cmd_names = {c["command"] for c in cmds}
        specialists = ["go", "py", "ts", "rev", "devops", "db", "docs", "qa"]
        for cmd in specialists:
            assert cmd in cmd_names, "/%s specialist shortcut was removed (regression)" % cmd

    def test_system_commands_not_removed(self):
        """Regression: /start /mode /status /queue /cancel /help must remain."""
        cmds = self._capture_commands()
        cmd_names = {c["command"] for c in cmds}
        system = ["start", "mode", "status", "queue", "cancel", "help"]
        for cmd in system:
            assert cmd in cmd_names, "/%s system command was removed (regression)" % cmd


# ============================================================================
# T6 — Invariant test: all four surfaces share exactly set(MODES)
# ============================================================================

class TestModeInvariant:
    """The one test to rule them all — catches any future key drift."""

    def _inline_kb_keys(self):
        kb = _panel_mod.mode_keyboard()
        return {
            btn["callback_data"][len("mode:"):]
            for row in kb["inline_keyboard"]
            for btn in row
            if btn["callback_data"].startswith("mode:")
        }

    def _reply_kb_keys(self):
        kb = _panel_mod.persistent_reply_keyboard()
        keys = set()
        for row in kb["keyboard"]:
            for btn in row:
                for k, m in MODES.items():
                    if btn["text"] == "%s %s" % (m["emoji"], k):
                        keys.add(k)
        return keys

    def _reply_map_mode_keys(self):
        return {
            cmd.lstrip("/")
            for cmd in _handlers_mod._REPLY_BUTTON_MAP.values()
            if cmd not in ("/status", "/help")
        }

    def _bot_commands_mode_keys(self):
        """Extract mode-command keys from setup_bot_commands() via mocked API."""
        import tgbridge.commands as _cmd_mod

        captured = []

        def fake_api_json(method, payload, timeout=30):
            if method == "setMyCommands":
                captured.append(payload["commands"])
            return {"ok": True, "result": {}}

        with patch.object(_cmd_mod, "api_json", fake_api_json):
            _cmd_mod.setup_bot_commands()

        if not captured:
            return set()
        cmds = captured[0]
        return {c["command"] for c in cmds} & set(MODES)

    def test_all_surfaces_equal_modes_keyset(self):
        expected = set(MODES)
        surfaces = {
            "CONTEXTS":                    set(CONTEXTS),
            "mode_keyboard()":             self._inline_kb_keys(),
            "persistent_reply_keyboard()": self._reply_kb_keys(),
            "_REPLY_BUTTON_MAP":           self._reply_map_mode_keys(),
            "setup_bot_commands()":        self._bot_commands_mode_keys(),
        }
        for name, keys in surfaces.items():
            assert keys == expected, (
                "Surface %r: keys %r != set(MODES) %r\n"
                "Extra: %r  Missing: %r" % (
                    name, keys, expected,
                    keys - expected, expected - keys,
                )
            )


# ============================================================================
# T10 — _done_ping
# ============================================================================

class TestDonePing:

    def _make_task(self, state="running", label="test", started_at=None):
        t = _state_mod.Task(
            id=uuid.uuid4().hex[:8],
            key="dev",
            label=label,
            prompt="hello",
            cwd="/tmp",
            agent=None,
            reply_to=42,
            kind="claude",
            state=state,
            enqueued_at=time.monotonic(),
            started_at=started_at if started_at is not None else time.monotonic() - 5,
            proc=None,
        )
        return t

    def test_done_ping_sent_on_completion(self, monkeypatch):
        sent = []
        monkeypatch.setattr(_tgapi_mod, "send",
                            lambda text, reply_to=None, reply_markup=None: sent.append(text))
        task = self._make_task(state="running", label="mytask")
        _workers_mod._done_ping(task, None)
        assert len(sent) == 1
        msg = sent[0]
        assert "✅" in msg
        assert "mytask" in msg

    def test_done_ping_includes_elapsed(self, monkeypatch):
        sent = []
        monkeypatch.setattr(_tgapi_mod, "send",
                            lambda text, reply_to=None, reply_markup=None: sent.append(text))
        task = self._make_task(state="running")
        _workers_mod._done_ping(task, None)
        assert "⏱" in sent[0], "Elapsed time indicator missing from ping"
        assert "s" in sent[0], "Elapsed seconds not in ping"

    def test_done_ping_suppressed_on_cancelling(self, monkeypatch):
        """Suppression must work via the actual worker flow: capture state BEFORE finish().

        The bug was that _state.finish() sets task.state = 'done' before _done_ping
        checks task.state.  The fix captures was_cancelling before finish() and passes
        it as a flag.  This test reproduces the real call sequence to prevent regression.
        """
        sent = []
        monkeypatch.setattr(_tgapi_mod, "send",
                            lambda text, reply_to=None, reply_markup=None: sent.append(text))

        task = self._make_task(state="cancelling")

        was_cancelling = (task.state == "cancelling")
        task.state = "done"
        _workers_mod._done_ping(task, None, was_cancelling=was_cancelling)

        assert len(sent) == 0, (
            "Ping must be suppressed when task was cancelling before finish()."
        )

    def test_done_ping_includes_next_task_label(self, monkeypatch):
        sent = []
        monkeypatch.setattr(_tgapi_mod, "send",
                            lambda text, reply_to=None, reply_markup=None: sent.append(text))
        task = self._make_task(state="running", label="first")
        nxt = self._make_task(state="queued", label="second")
        _workers_mod._done_ping(task, nxt)
        assert "▶" in sent[0]
        assert "second" in sent[0]

    def test_done_ping_without_next_no_next_label(self, monkeypatch):
        sent = []
        monkeypatch.setattr(_tgapi_mod, "send",
                            lambda text, reply_to=None, reply_markup=None: sent.append(text))
        task = self._make_task(state="running", label="only")
        _workers_mod._done_ping(task, None)
        assert "▶" not in sent[0]


# ============================================================================
# T11 — _panel_text() with IPC file states
# ============================================================================

class TestPanelTextWaiting:

    def test_panel_text_shows_pending_confirmation(self, tmp_path, monkeypatch):
        pending_file = tmp_path / "approval.pending"
        pending_file.write_text("data")
        monkeypatch.setattr(_panel_mod, "PENDING",  str(pending_file))
        monkeypatch.setattr(_panel_mod, "ASK_PEND", str(tmp_path / "ask.pending"))
        text = _panel_mod._panel_text()
        assert "Ожидаю" in text, "Expected 'Ожидаю' in panel text when PENDING exists"
        assert "🟡" in text

    def test_panel_text_shows_ask_pending(self, tmp_path, monkeypatch):
        ask_file = tmp_path / "ask.pending"
        ask_file.write_text("question")
        monkeypatch.setattr(_panel_mod, "PENDING",  str(tmp_path / "approval.pending"))
        monkeypatch.setattr(_panel_mod, "ASK_PEND", str(ask_file))
        text = _panel_mod._panel_text()
        assert "Ожидаю" in text, "Expected 'Ожидаю' in panel text when ASK_PEND exists"
        assert "🟡" in text

    def test_panel_text_normal_when_no_ipc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_panel_mod, "PENDING",  str(tmp_path / "no_pending"))
        monkeypatch.setattr(_panel_mod, "ASK_PEND", str(tmp_path / "no_ask"))
        text = _panel_mod._panel_text()
        assert "🟡" not in text
        assert "🟢" in text or "🔴" in text

    def test_panel_text_pending_priority_over_ask(self, tmp_path, monkeypatch):
        """PENDING takes priority over ASK_PEND."""
        pending_file = tmp_path / "approval.pending"
        ask_file = tmp_path / "ask.pending"
        pending_file.write_text("p")
        ask_file.write_text("a")
        monkeypatch.setattr(_panel_mod, "PENDING",  str(pending_file))
        monkeypatch.setattr(_panel_mod, "ASK_PEND", str(ask_file))
        text = _panel_mod._panel_text()
        assert "подтверждения" in text, "PENDING should show 'подтверждения' not 'ответа'"
