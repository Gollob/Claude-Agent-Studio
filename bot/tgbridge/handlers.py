# tgbridge/handlers.py — message and callback handlers.
# Depends on: state, tgapi, panel, workers, process, config.
#
# Cross-module calls use module-qualified references (import tgbridge.X as _X)
# so that monkeypatch.setattr(tgbridge.X, "name", mock) is effective on the
# runtime call path.  from-imports are kept only for config constants and
# the lock/registry dicts (which are never patched themselves — their contents
# are mutated directly by tests).
import json
import os
import sys
import time

import tgbridge.panel as _panel
import tgbridge.process as _process
import tgbridge.tgapi as _tgapi
import tgbridge.workers as _workers
from tgbridge.config import (
    CHAT, CONTEXTS, DECISION, HELP, MODES, PENDING, SHORTCUTS,
)
from tgbridge.state import Task, lock, queues, running, tasks_by_id


# ---------------------------------------------------------------------------
# /cancel command helpers
# ---------------------------------------------------------------------------

def _cancel_task(task: Task) -> None:
    """Kill the process of a task whose state is already 'cancelling'.

    Reads task.proc under lock to avoid a race with _run_once setting it.
    Must be called OUTSIDE the main lock (kill itself is not under lock).
    """
    with lock:
        proc = task.proc
    if proc is not None:
        _process._kill_tree(proc)


def _cancel_key(key: str, notify_reply: "int | None" = None) -> str:
    """Cancel active task for key and clear its queue. Returns summary string.

    BLOCKER-1 fix: under lock we only mark active as 'cancelling' and clear the
    queue — we do NOT remove active from running.  The sole path that removes
    a task from running is worker finally → finish(key).  Because the queue is
    empty by the time finish() runs, it will find nothing to start next.
    This preserves the registry invariant and prevents double-start.
    """
    lines = []
    with lock:
        active = running.get(key)
        queued_tasks = list(queues.get(key, []))
        if key in queues:
            del queues[key]
        for t in queued_tasks:
            tasks_by_id.pop(t.id, None)
        if active is not None:
            active.state = "cancelling"
    if active is not None:
        _cancel_task(active)
        lines.append("✋ Отменено: [%s]" % active.label)
    else:
        lines.append("Нет активной задачи для ключа «%s»." % key)
    if queued_tasks:
        lines.append("Очередь ключа «%s» очищена (%d задач)." % (key, len(queued_tasks)))
    _panel.refresh_panel()
    return "\n".join(lines)


def _cancel_queued_by_id(task_id: str) -> str:
    """Cancel a specific queued (not running) task by id."""
    with lock:
        task = tasks_by_id.get(task_id)
        if task is None:
            return "Задача %s не найдена." % task_id
        if task.state == "running":
            return None  # signal caller to do _cancel_key
        q = queues.get(task.key)
        if q is not None:
            try:
                q.remove(task)
            except ValueError:
                pass
            if not q:
                del queues[task.key]
        tasks_by_id.pop(task_id, None)
        task.state = "done"
    _panel.refresh_panel()
    return "🗑 Удалено из очереди: [%s]" % task.label


def handle_cancel(arg: str, reply_to: "int | None") -> None:
    """Handle /cancel [key|all|<id>] command."""
    arg = arg.strip()
    if not arg:
        key = _panel.get_mode()
        msg = _cancel_key(key)
        _tgapi.send(msg, reply_to)
        return
    if arg == "all":
        with lock:
            active_tasks = list(running.values())
            all_queued: "list[Task]" = []
            for q in queues.values():
                all_queued.extend(q)
            if not active_tasks and not all_queued:
                pass
            else:
                for t in active_tasks:
                    t.state = "cancelling"
                for t in all_queued:
                    tasks_by_id.pop(t.id, None)
                queues.clear()
        if not active_tasks and not all_queued:
            _tgapi.send("Нет активных задач.", reply_to)
            return
        for t in active_tasks:
            _cancel_task(t)
        lines = []
        for t in active_tasks:
            lines.append("✋ Отменено: [%s]" % t.label)
        if all_queued:
            lines.append("Очередь очищена (%d задач)." % len(all_queued))
        _panel.refresh_panel()
        _tgapi.send("\n".join(lines), reply_to)
        return
    with lock:
        is_key = arg in running or arg in queues
    if is_key:
        msg = _cancel_key(arg)
        _tgapi.send(msg, reply_to)
        return
    result = _cancel_queued_by_id(arg)
    if result is None:
        with lock:
            task = tasks_by_id.get(arg)
        if task:
            msg = _cancel_key(task.key)
        else:
            msg = "Задача %s не найдена." % arg
        _tgapi.send(msg, reply_to)
    else:
        _tgapi.send(result, reply_to)


# ---------------------------------------------------------------------------
# /queue command
# ---------------------------------------------------------------------------

def handle_queue(reply_to: "int | None") -> None:
    """Show snapshot of task registry with inline cancel buttons."""
    with lock:
        run_snap = dict(running)
        queue_snap = {k: list(v) for k, v in queues.items()}

    if not run_snap and not queue_snap:
        _tgapi.send("✅ Очередь пуста, задач нет.", reply_to)
        return

    now = time.monotonic()
    lines = ["📋 Задачи:"]
    cancel_buttons = []

    for key, task in run_snap.items():
        elapsed = int(now - (task.started_at or now))
        m, s = divmod(elapsed, 60)
        lines.append("🔴 [%s] %s — %dm%ds" % (key, task.label, m, s))
        cancel_buttons.append({"text": "✋ %s" % task.label, "callback_data": "cancel:%s" % task.id})
        q = queue_snap.get(key, [])
        for pos, qt in enumerate(q, 1):
            lines.append("   %d. [%s] %s" % (pos, key, qt.label))
            cancel_buttons.append({"text": "🗑 %s" % qt.label, "callback_data": "cancel:%s" % qt.id})

    for key, q in queue_snap.items():
        if key not in run_snap:
            for pos, qt in enumerate(q, 1):
                lines.append("   %d. [%s] %s" % (pos, key, qt.label))
                cancel_buttons.append({"text": "🗑 %s" % qt.label, "callback_data": "cancel:%s" % qt.id})

    kb_rows = []
    for i in range(0, len(cancel_buttons), 3):
        kb_rows.append(cancel_buttons[i:i+3])
    kb_rows.append([{"text": "✋ Отменить всё", "callback_data": "cancel:all"}])
    reply_markup = {"inline_keyboard": kb_rows}

    text = "\n".join(lines)
    if not text or not text.strip():
        text = "(пустой ответ)"
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for idx, ch in enumerate(chunks):
        try:
            payload = {"chat_id": CHAT, "text": ch}
            if reply_to and idx == 0:
                payload["reply_to_message_id"] = reply_to
            if idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            _tgapi.api_json("sendMessage", payload)
        except Exception as e:
            sys.stderr.write("queue send error: %s\n" % e)


# ---------------------------------------------------------------------------
# Text routing
# ---------------------------------------------------------------------------

_REPLY_BUTTON_MAP = {
    **{"%s %s" % (m["emoji"], k): "/%s" % k for k, m in MODES.items()},
    "📟 Панель": "/status",
    "❓ Help":   "/help",
}


def handle_text(text, reply_to):
    from tgbridge.panel import mode_keyboard

    t = text.strip()

    if t in _REPLY_BUTTON_MAP:
        t = _REPLY_BUTTON_MAP[t]

    if t.startswith("/"):
        parts = t[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "mode":
            from tgbridge.commands import show_menu
            show_menu(reply_to)
            return
        if cmd == "status":
            _panel.refresh_panel()
            _tgapi.send("Текущий режим: %s" % _panel.get_mode(), reply_to,
                        _panel.persistent_reply_keyboard())
            return
        if cmd in ("help", "start"):
            _tgapi.send(HELP, reply_to)
            return
        if cmd == "queue":
            handle_queue(reply_to)
            return
        if cmd == "cancel":
            handle_cancel(rest, reply_to)
            return
        if cmd in CONTEXTS:
            if rest:
                _workers.dispatch(rest, CONTEXTS[cmd], None, reply_to, busy_key=cmd, label=cmd)
            else:
                _panel.set_mode(cmd)
                _panel.refresh_panel()
                _tgapi.send("✅ Режим переключён: %s" % cmd, reply_to,
                            _panel.persistent_reply_keyboard())
            return
        if cmd in SHORTCUTS:
            agent = SHORTCUTS[cmd]
            if not rest:
                _tgapi.send("Нужен текст задачи: /%s <что сделать>" % cmd, reply_to)
                return
            _workers.dispatch(rest, CONTEXTS["dev"], agent, reply_to,
                              busy_key="ag:" + agent, label=agent)
            return
    mode = _panel.get_mode()
    _workers.dispatch(text, CONTEXTS[mode], None, reply_to, busy_key=mode, label=mode)


# ---------------------------------------------------------------------------
# Approval callback
# ---------------------------------------------------------------------------

def handle_approval_callback(action: str, req_id: str, cq_id: str, msg_id: int) -> None:
    """Process inline button press for approval request."""
    try:
        _tgapi.api("answerCallbackQuery", {"callback_query_id": cq_id})
    except Exception:
        pass

    pending = None
    try:
        with open(PENDING) as f:
            pending = json.load(f)
    except Exception:
        try:
            _tgapi.api_json("editMessageText", {
                "chat_id": CHAT,
                "message_id": msg_id,
                "text": "⚠️ Устаревший запрос (нет активного pending).",
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception:
            pass
        return

    if pending.get("id") != req_id:
        try:
            _tgapi.api_json("editMessageText", {
                "chat_id": CHAT,
                "message_id": msg_id,
                "text": "⚠️ Устаревший запрос — id не совпадает.",
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception:
            pass
        return

    try:
        with open(DECISION, "w") as f:
            f.write(action)
    except Exception as e:
        sys.stderr.write("decision write error: %s\n" % e)
        return

    try:
        os.remove(PENDING)
    except OSError:
        pass

    result_text = (
        ("✅ Разрешено" if action == "allow" else "⛔ Запрещено")
        + "\n`" + pending.get("cmd", "")[:200] + "`"
    )
    try:
        _tgapi.api_json("editMessageText", {
            "chat_id": CHAT,
            "message_id": msg_id,
            "text": result_text,
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception as e:
        sys.stderr.write("edit approval message error: %s\n" % e)
