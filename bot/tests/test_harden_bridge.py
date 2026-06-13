"""Tests for harden-tg-bot block 5 (tasks.md).

Covers:
  5.1  Task registry: enqueue, queuing, finish / FIFO, exception-safety (finally).
  5.2  Kill-tree: process group with child subprocess; no orphans after _kill_tree.
  5.3  Command parsing: /queue, /cancel variants; cancel: callbacks.
  5.4  Env-scrub: child_env() allowlist/denylist; mask_secrets() both token formats.
  5.5  Regression: existing tests pass (not re-run here, just the new ones are clean).

Isolation rules:
  - No real Telegram API calls (api / api_json / send patched everywhere).
  - No real claude invoked.
  - kill-tree test: real subprocess used (shell-script stub spawning sleep 300),
    cleaned up in teardown via os.killpg(pgid, SIGKILL) even on test failure.

Patching strategy (ADR-001 / refactor/modular):
  workers.py and handlers.py use module-qualified references for all patchable
  cross-module symbols (_state.finish, _cr.run_claude, _tgapi.send, etc.).
  Therefore patches MUST target the defining module, not the entry re-export:
    - tgbridge.state.finish / tgbridge.state.enqueue
    - tgbridge.claude_runner.run_claude
    - tgbridge.tgapi.send / tgbridge.tgapi.api / tgbridge.tgapi.api_json
    - tgbridge.panel.refresh_panel / tgbridge.panel.get_mode
    - tgbridge.workers._start_worker_thread / tgbridge.workers.dispatch
    - tgbridge.handlers.handle_queue / tgbridge.handlers.handle_cancel
    - tgbridge.handlers._cancel_task
  Patching tgbridge.<module>.<name> IS effective because the runtime call path
  resolves through the module object (_state.finish → tgbridge.state.__dict__['finish']).
"""

import collections
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load telegram-bridge.py (importlib because filename has a dash)
# ---------------------------------------------------------------------------

REPO_DIR    = Path(__file__).parent.parent
BRIDGE_PATH = REPO_DIR / "telegram-bridge.py"


def _load_bridge():
    """Fresh import of telegram-bridge.py with dummy Telegram credentials."""
    dummy = {"TELEGRAM_TOKEN": "DUMMY_HARDEN_TOKEN", "TELEGRAM_CHAT_ID": "9999"}
    saved = {}
    for k, v in dummy.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location("telegram_bridge", BRIDGE_PATH)
        mod = importlib.util.module_from_spec(spec)
        with patch("os.makedirs"):
            spec.loader.exec_module(mod)
    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
    return mod


# Single shared import for all tests (registry state is reset per test via fixtures)
_bridge = _load_bridge()

# Direct module references used for targeted patching.
# These are the modules where patchable symbols are defined and resolved at runtime.
import tgbridge.claude_runner as _cr_mod
import tgbridge.handlers as _handlers_mod
import tgbridge.panel as _panel_mod
import tgbridge.state as _state_mod
import tgbridge.tgapi as _tgapi_mod
import tgbridge.workers as _workers_mod

# ---------------------------------------------------------------------------
# Helper: clear registry between tests
# ---------------------------------------------------------------------------

def _clear_registry():
    """Reset the shared task registry state in the module under test."""
    with _bridge.lock:
        _bridge.running.clear()
        _bridge.queues.clear()
        _bridge.tasks_by_id.clear()


def _make_task(key="dev", label="test", prompt="hello", kind="claude"):
    return _bridge.Task(
        id=uuid.uuid4().hex[:8],
        key=key,
        label=label,
        prompt=prompt,
        cwd="/tmp",
        agent=None,
        reply_to=None,
        kind=kind,
        state="queued",
        enqueued_at=time.monotonic(),
        started_at=None,
        proc=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure the registry is empty before and after each test."""
    _clear_registry()
    yield
    _clear_registry()


@pytest.fixture()
def mock_api(monkeypatch):
    """Silence api / api_json / send on the tgapi module (effective on runtime path)."""
    calls = []

    def _api(method, params=None, timeout=80):
        calls.append(("api", method, params))
        return {"ok": True, "result": []}

    def _api_json(method, payload, timeout=30):
        calls.append(("api_json", method, payload))
        return {"ok": True, "result": {"message_id": 1}}

    def _send(text, reply_to=None, reply_markup=None):
        calls.append(("send", text))

    # Patch on the defining module (tgbridge.tgapi) — workers.py and
    # handlers.py both call _tgapi.send / _tgapi.api / _tgapi.api_json
    # via module-qualified references, so this patch is effective.
    monkeypatch.setattr(_tgapi_mod, "api",      _api)
    monkeypatch.setattr(_tgapi_mod, "api_json", _api_json)
    monkeypatch.setattr(_tgapi_mod, "send",     _send)
    # Also update the entry-module re-exports so tests that access _bridge.send etc. see the mock.
    monkeypatch.setattr(_bridge, "api",      _api)
    monkeypatch.setattr(_bridge, "api_json", _api_json)
    monkeypatch.setattr(_bridge, "send",     _send)
    return calls


# ============================================================================
# 5.1  Task registry / FIFO queue
# ============================================================================

class TestRegistry:

    def test_enqueue_free_key_returns_started(self):
        """Enqueue on a free key → started=True, pos=0, task in running."""
        t = _make_task(key="dev")
        started, pos = _bridge.enqueue(t)
        assert started is True
        assert pos == 0
        assert _bridge.running.get("dev") is t
        assert t.state == "running"

    def test_enqueue_busy_key_goes_to_queue(self):
        """Enqueue second task on occupied key → started=False, pos=1, in queues."""
        t1 = _make_task(key="dev", label="first")
        t2 = _make_task(key="dev", label="second")
        _bridge.enqueue(t1)
        started, pos = _bridge.enqueue(t2)
        assert started is False
        assert pos == 1
        assert _bridge.queues["dev"][0] is t2
        assert t2.state == "queued"  # not running yet

    def test_enqueue_second_and_third_positions(self):
        """Third task gets position 2."""
        t1 = _make_task(key="dev", label="first")
        t2 = _make_task(key="dev", label="second")
        t3 = _make_task(key="dev", label="third")
        _bridge.enqueue(t1)
        _, pos2 = _bridge.enqueue(t2)
        _, pos3 = _bridge.enqueue(t3)
        assert pos2 == 1
        assert pos3 == 2

    def test_task_not_lost_when_queued(self):
        """Queued task is registered in tasks_by_id."""
        t1 = _make_task(key="dev", label="first")
        t2 = _make_task(key="dev", label="second")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)
        assert t2.id in _bridge.tasks_by_id

    def test_finish_removes_running_and_advances_fifo(self):
        """finish() pops the running task and promotes the next from queue."""
        t1 = _make_task(key="dev", label="first")
        t2 = _make_task(key="dev", label="second")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)

        nxt = _bridge.finish("dev")
        assert nxt is t2
        assert t2.state == "running"
        assert _bridge.running["dev"] is t2
        assert "dev" not in _bridge.queues  # queue empty → key removed

    def test_finish_clears_done_task_from_tasks_by_id(self):
        """Finished running task is removed from tasks_by_id."""
        t = _make_task(key="dev")
        _bridge.enqueue(t)
        _bridge.finish("dev")
        assert t.id not in _bridge.tasks_by_id

    def test_finish_on_empty_queue_returns_none(self):
        """finish() with no queued tasks returns None."""
        t = _make_task(key="dev")
        _bridge.enqueue(t)
        nxt = _bridge.finish("dev")
        assert nxt is None
        assert "dev" not in _bridge.running

    def test_finish_on_nonexistent_key_returns_none(self):
        """finish() on a key with no running task is a no-op."""
        nxt = _bridge.finish("nonexistent")
        assert nxt is None

    def test_exception_in_worker_calls_finish(self, monkeypatch, mock_api):
        """If worker raises, finish() must still be called (finally block).

        Patches tgbridge.state.finish (where _claude_worker resolves it via
        _state.finish) — this is the effective patch target.
        """
        finish_calls = []
        original_finish = _state_mod.finish

        def tracking_finish(key):
            finish_calls.append(key)
            return original_finish(key)

        # Patch on the defining module — workers._claude_worker calls _state.finish(...)
        monkeypatch.setattr(_state_mod, "finish", tracking_finish)
        monkeypatch.setattr(_cr_mod, "run_claude",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        t = _make_task(key="dev", label="exploder")
        # Enqueue and run the claude worker synchronously
        _bridge.enqueue(t)

        worker_thread = threading.Thread(target=_workers_mod._claude_worker, args=(t,))
        worker_thread.start()
        worker_thread.join(timeout=5)

        assert "dev" in finish_calls, "finish(key) was not called despite worker exception"

    def test_exception_in_worker_releases_key(self, monkeypatch, mock_api):
        """After worker exception, key is no longer in running (key freed)."""
        monkeypatch.setattr(_cr_mod, "run_claude",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        t = _make_task(key="dev", label="exploder2")
        _bridge.enqueue(t)

        worker_thread = threading.Thread(target=_workers_mod._claude_worker, args=(t,))
        worker_thread.start()
        worker_thread.join(timeout=5)

        with _bridge.lock:
            assert "dev" not in _bridge.running, "key still in running after worker exception"

    def test_exception_in_worker_advances_queue(self, monkeypatch, mock_api):
        """After worker exception, next queued task starts (FIFO continues).

        Patches tgbridge.workers._start_worker_thread (where _claude_worker
        resolves it via direct call in same module) — the patch on the
        workers module is effective because _start_worker_thread is resolved
        as a global in workers.py's namespace.
        """
        started_tasks = []

        def patched_start_worker(task):
            started_tasks.append(task)

        # _claude_worker calls _start_worker_thread directly (same module global).
        # Patching the workers module dict makes the call see the mock.
        monkeypatch.setattr(_workers_mod, "_start_worker_thread", patched_start_worker)
        monkeypatch.setattr(_cr_mod, "run_claude",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        t1 = _make_task(key="dev", label="first")
        t2 = _make_task(key="dev", label="second")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)

        worker_thread = threading.Thread(target=_workers_mod._claude_worker, args=(t1,))
        worker_thread.start()
        worker_thread.join(timeout=5)

        assert len(started_tasks) == 1, "Next task in queue not started after worker exception"
        assert started_tasks[0] is t2

    def test_different_keys_independent(self):
        """Tasks on different keys don't interfere with each other."""
        t_dev = _make_task(key="dev", label="dev-task")
        t_med = _make_task(key="med", label="med-task")
        started_dev, pos_dev = _bridge.enqueue(t_dev)
        started_med, pos_med = _bridge.enqueue(t_med)
        assert started_dev is True
        assert started_med is True
        assert _bridge.running["dev"] is t_dev
        assert _bridge.running["med"] is t_med


# ============================================================================
# 5.2  Kill-tree: no orphans
# ============================================================================

# Shell stub: spawn a child sleep 300, print parent PID and child PID,
# then wait indefinitely (acting as the "claude" process).
_STUB_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    sleep 300 &
    CHILD_PID=$!
    echo "parent=$$"
    echo "child=$CHILD_PID"
    wait
""")


@pytest.fixture()
def stub_process(tmp_path):
    """Spawn the shell stub in a new session. Yields (proc, parent_pid, child_pid, pgid).
    Teardown: SIGKILL the entire process group to guarantee no orphans.
    """
    stub_file = tmp_path / "fake_claude.sh"
    stub_file.write_text(_STUB_SCRIPT)
    stub_file.chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(stub_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    # Read both PID lines from stdout (with timeout guard)
    lines = {}
    deadline = time.monotonic() + 5.0
    while len(lines) < 2 and time.monotonic() < deadline:
        line = proc.stdout.readline().strip()
        if "=" in line:
            k, v = line.split("=", 1)
            lines[k] = int(v)
        time.sleep(0.05)

    parent_pid = lines.get("parent")
    child_pid  = lines.get("child")
    pgid       = os.getpgid(proc.pid) if proc.poll() is None else None

    yield proc, parent_pid, child_pid, pgid

    # Teardown: kill entire group regardless of test outcome
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    proc.communicate(timeout=2)


def _proc_alive(pid: int) -> bool:
    """Return True if a process with the given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _pgid_alive(pgid: int) -> bool:
    """Return True if any process in the process group exists."""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _wait_dead(check_fn, timeout=6.0, interval=0.05):
    """Poll until check_fn() returns False (process gone) or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not check_fn():
            return True
        time.sleep(interval)
    return False


class TestKillTree:

    def test_kill_tree_kills_parent_and_child(self, stub_process):
        """_kill_tree(proc) kills both parent and its child subprocess."""
        proc, parent_pid, child_pid, pgid = stub_process

        assert parent_pid is not None, "stub did not print parent PID"
        assert child_pid  is not None, "stub did not print child PID"
        assert pgid       is not None, "process group not found"

        # Both processes must be alive before kill
        assert _proc_alive(parent_pid), "parent not alive before kill"
        assert _proc_alive(child_pid),  "child not alive before kill"

        _bridge._kill_tree(proc, grace=2)

        # Wait for both to die (with generous deadline to avoid flakiness)
        assert _wait_dead(lambda: _pgid_alive(pgid), timeout=6), \
            "process group still alive after _kill_tree"
        assert _wait_dead(lambda: _proc_alive(child_pid), timeout=6), \
            "child process still alive after _kill_tree — orphan!"
        assert _wait_dead(lambda: _proc_alive(parent_pid), timeout=6), \
            "parent process still alive after _kill_tree"

    def test_kill_tree_idempotent_already_dead(self, stub_process):
        """_kill_tree on an already-dead process must not raise."""
        proc, parent_pid, child_pid, pgid = stub_process

        _bridge._kill_tree(proc, grace=2)
        _wait_dead(lambda: _pgid_alive(pgid), timeout=6)

        # Second call — should be a no-op
        try:
            _bridge._kill_tree(proc, grace=2)
        except Exception as exc:
            pytest.fail(f"_kill_tree raised on dead process: {exc}")

    def test_kill_tree_no_orphan_after_cancel(self, stub_process, monkeypatch, mock_api):
        """Simulated /cancel flow: task.proc set, _cancel_task called — no orphans."""
        proc, parent_pid, child_pid, pgid = stub_process

        # Build a fake task holding this process
        task = _make_task(key="dev", label="cancel-test")
        task.state = "cancelling"
        task.proc = proc
        _bridge.running["dev"] = task
        _bridge.tasks_by_id[task.id] = task

        # _cancel_task reads task.proc under lock and calls _kill_tree
        _bridge._cancel_task(task)

        assert _wait_dead(lambda: _pgid_alive(pgid), timeout=6), \
            "process group alive after _cancel_task — orphan!"
        assert _wait_dead(lambda: _proc_alive(child_pid), timeout=6), \
            "child process alive after _cancel_task — orphan!"


# ============================================================================
# 5.3  Command parsing and cancel callbacks
# ============================================================================

class TestCommandParsing:
    """Tests for /queue, /cancel, callback routing via handle_text / handle_cancel."""

    def test_handle_text_queue_command(self, monkeypatch, mock_api):
        """/queue command reaches handle_queue.

        handle_text calls handle_queue as a module-local name — patch on
        tgbridge.handlers is effective.
        """
        called = []
        monkeypatch.setattr(_handlers_mod, "handle_queue",
                            lambda reply_to: called.append(reply_to))
        _bridge.handle_text("/queue", reply_to=1)
        assert called == [1]

    def test_handle_text_cancel_no_arg(self, monkeypatch, mock_api):
        """/cancel with no argument calls handle_cancel with empty string."""
        calls = []
        monkeypatch.setattr(_handlers_mod, "handle_cancel",
                            lambda arg, reply_to: calls.append((arg, reply_to)))
        _bridge.handle_text("/cancel", reply_to=2)
        assert calls == [("", 2)]

    def test_handle_text_cancel_with_key(self, monkeypatch, mock_api):
        """/cancel dev passes 'dev' to handle_cancel."""
        calls = []
        monkeypatch.setattr(_handlers_mod, "handle_cancel",
                            lambda arg, reply_to: calls.append((arg, reply_to)))
        _bridge.handle_text("/cancel dev", reply_to=3)
        assert calls[0][0] == "dev"

    def test_handle_text_cancel_all(self, monkeypatch, mock_api):
        """/cancel all passes 'all' to handle_cancel."""
        calls = []
        monkeypatch.setattr(_handlers_mod, "handle_cancel",
                            lambda arg, reply_to: calls.append((arg, reply_to)))
        _bridge.handle_text("/cancel all", reply_to=4)
        assert calls[0][0] == "all"

    def test_handle_cancel_no_arg_cancels_current_mode(self, monkeypatch, mock_api):
        """/cancel with no arg cancels the current mode's active task."""
        monkeypatch.setattr(_panel_mod, "get_mode", lambda: "sys")
        # Put a task in running for 'sys'
        t = _make_task(key="sys", label="sys-task")
        t.state = "running"
        _bridge.running["sys"] = t
        _bridge.tasks_by_id[t.id] = t
        monkeypatch.setattr(_handlers_mod, "_cancel_task", lambda task: None)
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel("", reply_to=None)
        assert t.state == "cancelling"

    def test_handle_cancel_key_marks_cancelling(self, monkeypatch, mock_api):
        """handle_cancel('dev') marks running dev task as cancelling."""
        t = _make_task(key="dev", label="dev-running")
        t.state = "running"
        _bridge.running["dev"] = t
        _bridge.tasks_by_id[t.id] = t
        monkeypatch.setattr(_handlers_mod, "_cancel_task", lambda task: None)
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel("dev", reply_to=None)
        assert t.state == "cancelling"

    def test_handle_cancel_key_clears_queue(self, monkeypatch, mock_api):
        """handle_cancel('dev') clears the dev queue."""
        t1 = _make_task(key="dev", label="running")
        t2 = _make_task(key="dev", label="queued-1")
        t3 = _make_task(key="dev", label="queued-2")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)
        _bridge.enqueue(t3)
        monkeypatch.setattr(_handlers_mod, "_cancel_task", lambda task: None)
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel("dev", reply_to=None)
        with _bridge.lock:
            assert "dev" not in _bridge.queues

    def test_handle_cancel_all_marks_all_running_cancelling(self, monkeypatch, mock_api):
        """/cancel all marks every running task as cancelling."""
        t_dev = _make_task(key="dev", label="dev")
        t_med = _make_task(key="med", label="med")
        _bridge.enqueue(t_dev)
        _bridge.enqueue(t_med)
        killed = []
        monkeypatch.setattr(_handlers_mod, "_cancel_task",
                            lambda task: killed.append(task))
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel("all", reply_to=None)
        assert t_dev.state == "cancelling"
        assert t_med.state == "cancelling"
        assert len(killed) == 2

    def test_handle_cancel_all_clears_all_queues(self, monkeypatch, mock_api):
        """/cancel all clears all queues."""
        t1 = _make_task(key="dev", label="dev-run")
        t2 = _make_task(key="dev", label="dev-q")
        t3 = _make_task(key="med", label="med-run")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)
        _bridge.enqueue(t3)
        monkeypatch.setattr(_handlers_mod, "_cancel_task", lambda task: None)
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel("all", reply_to=None)
        with _bridge.lock:
            assert len(_bridge.queues) == 0

    def test_handle_cancel_all_no_tasks(self, mock_api):
        """handle_cancel('all') with empty registry sends 'no tasks' message."""
        sent = mock_api
        _bridge.handle_cancel("all", reply_to=None)
        # Should send a 'no active tasks' type message
        send_texts = [c[1] for c in sent if c[0] == "send"]
        assert any("нет" in t.lower() or "пуст" in t.lower() or "задач" in t.lower()
                   for t in send_texts), f"Expected 'no tasks' message, got: {send_texts}"

    def test_handle_cancel_by_task_id_queued(self, monkeypatch, mock_api):
        """handle_cancel(<id>) removes a specific queued task."""
        t1 = _make_task(key="dev", label="running")
        t2 = _make_task(key="dev", label="queued-target")
        _bridge.enqueue(t1)
        _bridge.enqueue(t2)
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        _bridge.handle_cancel(t2.id, reply_to=None)
        with _bridge.lock:
            assert t2.id not in _bridge.tasks_by_id
            q = _bridge.queues.get("dev", collections.deque())
            assert t2 not in q

    def test_cancel_callback_all_via_handle_cancel(self, monkeypatch, mock_api):
        """cancel:all callback arg parsing: 'cancel:all'.split(':',1)[1] == 'all'.

        Simulates the main loop extracting the arg from callback_data and passing
        it to handle_cancel.  We verify the extracted arg is correct and that
        handle_cancel processes it (sends 'no tasks' since registry is empty).
        """
        # Verify the arg extraction that main loop does
        cancel_arg = "cancel:all".split(":", 1)[1]
        assert cancel_arg == "all"
        # Verify handle_cancel processes 'all' on empty registry
        sent = mock_api
        _bridge.handle_cancel(cancel_arg, reply_to=10)
        send_texts = [c[1] for c in sent if c[0] == "send"]
        assert any("нет" in t.lower() or "задач" in t.lower() for t in send_texts), \
            "Expected 'no tasks' message for cancel:all on empty registry, got: %s" % send_texts

    def test_cancel_callback_id_via_handle_cancel(self, monkeypatch, mock_api):
        """cancel:<id> callback arg parsing: 'cancel:<id>'.split(':',1)[1] == '<id>'.

        Simulates the main loop extracting the arg from callback_data and passing
        it to handle_cancel.  We verify the extracted arg is correct and that
        handle_cancel processes it (sends 'not found' since no such task exists).
        """
        fake_id = "abc12345"
        # Verify the arg extraction that main loop does
        cancel_arg = ("cancel:%s" % fake_id).split(":", 1)[1]
        assert cancel_arg == fake_id
        # Verify handle_cancel processes the id on empty registry
        sent = mock_api
        _bridge.handle_cancel(cancel_arg, reply_to=11)
        send_texts = [c[1] for c in sent if c[0] == "send"]
        assert any(fake_id in t or "не найдена" in t for t in send_texts), \
            "Expected 'not found' message for cancel:<id>, got: %s" % send_texts

    def test_handle_queue_empty(self, mock_api):
        """handle_queue on empty registry sends 'empty' message via send()."""
        _bridge.handle_queue(reply_to=None)
        # Empty registry path uses _tgapi.send() — captured in mock_api
        sent_texts = [c[1] for c in mock_api if c[0] == "send"]
        assert len(sent_texts) >= 1, f"No message sent; calls={mock_api}"
        assert any("пуст" in t.lower() or "задач" in t.lower() for t in sent_texts), \
            f"Expected empty-queue text, got: {sent_texts}"

    def test_handle_queue_shows_running_task(self, mock_api):
        """handle_queue with a running task includes that task in output."""
        t = _make_task(key="dev", label="my-running-task")
        _bridge.enqueue(t)  # becomes running immediately

        _bridge.handle_queue(reply_to=None)
        sent = [c for c in mock_api if c[0] == "api_json" and c[1] == "sendMessage"]
        all_text = " ".join(c[2].get("text", "") for c in sent)
        assert "my-running-task" in all_text or "dev" in all_text


# ============================================================================
# 5.4  Env-scrub and mask_secrets
# ============================================================================

class TestEnvScrub:

    def test_child_env_excludes_telegram_token(self, monkeypatch):
        """child_env() must not contain TELEGRAM_TOKEN."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "SECRET_TOKEN_123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        env = _bridge.child_env()
        assert "TELEGRAM_TOKEN" not in env, \
            "TELEGRAM_TOKEN leaked into child environment"

    def test_child_env_excludes_telegram_chat_id(self, monkeypatch):
        """child_env() must not contain TELEGRAM_CHAT_ID."""
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "99887766")
        env = _bridge.child_env()
        assert "TELEGRAM_CHAT_ID" not in env, \
            "TELEGRAM_CHAT_ID leaked into child environment"

    def test_child_env_contains_path(self, monkeypatch):
        """child_env() must pass through PATH."""
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/custom")
        env = _bridge.child_env()
        assert "PATH" in env
        assert "/usr/bin" in env["PATH"]

    def test_child_env_contains_home(self, monkeypatch):
        """child_env() must pass through HOME."""
        monkeypatch.setenv("HOME", "/home/testuser")
        env = _bridge.child_env()
        assert "HOME" in env
        assert env["HOME"] == "/home/testuser"

    def test_child_env_contains_claude_token_when_set(self, monkeypatch):
        """child_env() includes CLAUDE_CODE_OAUTH_TOKEN when present in environment."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test-token")
        env = _bridge.child_env()
        assert "CLAUDE_CODE_OAUTH_TOKEN" in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test-token"

    def test_child_env_omits_unknown_var(self, monkeypatch):
        """child_env() drops variables not in the allowlist."""
        monkeypatch.setenv("MY_RANDOM_SECRET", "shh")
        env = _bridge.child_env()
        assert "MY_RANDOM_SECRET" not in env

    def test_child_env_passes_lc_prefixed_vars(self, monkeypatch):
        """child_env() passes LC_* locale variables."""
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        env = _bridge.child_env()
        assert "LC_ALL" in env

    def test_child_env_passes_xdg_prefixed_vars(self, monkeypatch):
        """child_env() passes XDG_* variables."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        env = _bridge.child_env()
        assert "XDG_RUNTIME_DIR" in env


class TestMaskSecrets:
    """mask_secrets tests use the actual TOKEN from tgbridge.config (the canonical source).

    In the modular architecture, TOKEN is a module-level constant in tgbridge.config
    (loaded once from TELEGRAM_TOKEN env at first import).  mask_secrets() in
    tgbridge.secrets also imports TOKEN from tgbridge.config, so both sides of
    the test use the same value regardless of which test file loaded first.
    """

    @property
    def _token(self) -> str:
        """Return the active TOKEN used by mask_secrets at runtime."""
        return _cr_mod.__spec__.parent and __import__("tgbridge.config", fromlist=["TOKEN"]).TOKEN

    def setup_method(self):
        """Verify that bridge TOKEN is a non-empty dummy (not a real Telegram token)."""
        tok = _bridge.TOKEN
        assert tok and "DUMMY" in tok.upper() or tok.startswith("DUMMY"), \
            "Expected a dummy token, got: %r (real credentials must not be used in tests)" % tok

    def test_mask_raw_token(self):
        """mask_secrets replaces raw token with ***."""
        tok = _bridge.TOKEN
        text = "Some error: token=%s appeared here" % tok
        result = _bridge.mask_secrets(text)
        assert tok not in result
        assert "***" in result

    def test_mask_bot_token_url(self):
        """mask_secrets replaces bot<TOKEN> URL pattern."""
        tok = _bridge.TOKEN
        text = "https://api.telegram.org/file/bot%s/file.ogg" % tok
        result = _bridge.mask_secrets(text)
        assert tok not in result

    def test_mask_generic_bot_url_pattern(self):
        """mask_secrets masks any /bot<long-token>/ URL regardless of token value."""
        text = "https://api.telegram.org/file/botABCDEF1234567890ABCDEF/doc.pdf"
        result = _bridge.mask_secrets(text)
        # Should be replaced with /bot***/
        assert "/bot***/" in result or "ABCDEF1234567890ABCDEF" not in result

    def test_mask_legitimate_text_unchanged(self):
        """mask_secrets does not alter text that contains neither token format."""
        text = "git status && pytest tests/ — all green"
        result = _bridge.mask_secrets(text)
        assert result == text

    def test_mask_empty_string(self):
        """mask_secrets on empty string returns empty string."""
        assert _bridge.mask_secrets("") == ""

    def test_mask_none_safe(self):
        """mask_secrets on None returns None (falsy guard)."""
        # The implementation checks 'if not text: return text'
        result = _bridge.mask_secrets(None)
        assert result is None

    def test_mask_both_formats_in_one_string(self):
        """mask_secrets masks both raw token and bot-URL token in one pass."""
        tok = _bridge.TOKEN
        text = (
            "raw token: %s and "
            "URL: https://api.telegram.org/bot%s/sendMessage"
        ) % (tok, tok)
        result = _bridge.mask_secrets(text)
        assert tok not in result

    def test_mask_does_not_corrupt_normal_url(self):
        """mask_secrets leaves normal HTTPS URLs unmodified."""
        text = "Check https://github.com/example/tg-bot for more info"
        result = _bridge.mask_secrets(text)
        assert "github.com/example/tg-bot" in result


# ============================================================================
# 5.6  Smoke: handle_text -> dispatch -> _start_worker_thread -> _claude_worker
# ============================================================================

class TestSmokePlainTextDispatch:
    """Integration smoke: plain (non-command) text reaches _claude_worker via
    handle_text -> _workers.dispatch -> _start_worker_thread -> _claude_worker.

    All I/O is mocked; run_claude returns immediately; test blocks on the
    worker thread and verifies the full round-trip completes without error.
    """

    def test_plain_text_reaches_claude_worker(self, monkeypatch, mock_api):
        """handle_text with plain text fires dispatch, starts worker, calls run_claude."""
        run_called = []
        send_texts = []

        monkeypatch.setattr(_cr_mod, "run_claude",
                            lambda prompt, cwd, agent, task: run_called.append(prompt) or "done")
        monkeypatch.setattr(_panel_mod, "get_mode", lambda: "dev")
        monkeypatch.setattr(_panel_mod, "refresh_panel", lambda: None)

        # Intercept _start_worker_thread to run it synchronously so the test
        # doesn't need an arbitrary sleep to wait for the daemon thread.
        original_start = _workers_mod._start_worker_thread

        def sync_start(task):
            """Run the worker synchronously in this thread (no daemon thread)."""
            _workers_mod._claude_worker(task)

        monkeypatch.setattr(_workers_mod, "_start_worker_thread", sync_start)

        _bridge.handle_text("тестовая задача", reply_to=42)

        assert run_called == ["тестовая задача"], (
            "run_claude was not called — dispatch/worker chain is broken"
        )
        # send() should have been called at least once ("Думаю…" + result)
        sent = [t for (kind, *rest) in mock_api if kind == "send" for t in rest]
        assert any("Думаю" in s for s in sent), "Expected 'Думаю…' send — worker didn't run"
        assert any("done" in s for s in sent), "Expected result 'done' in send calls"
