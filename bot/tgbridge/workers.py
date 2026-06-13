# tgbridge/workers.py — task worker threads and dispatch.
# Depends on: state, claude_runner, tgapi, panel, media, otel.
import json
import threading
import time

import tgbridge.claude_runner as _cr
import tgbridge.otel as _otel
import tgbridge.panel as _panel
import tgbridge.state as _state
import tgbridge.tgapi as _tgapi
from tgbridge.state import Task


def _start_worker_thread(task: Task) -> None:
    """Start the appropriate worker thread for a task (outside lock)."""
    if task.kind == "media":
        # Media tasks carry extra args in prompt field encoded as JSON
        from tgbridge.media import _media_worker_task
        args = json.loads(task.prompt)
        t = threading.Thread(
            target=_media_worker_task, args=(task,) + tuple(args),
            daemon=True,
        )
    else:
        t = threading.Thread(target=_claude_worker, args=(task,), daemon=True)
    t.start()


def _done_ping(task: Task, nxt: "Task | None", was_cancelling: bool = False) -> None:
    """Send a single completion ping for the task (N1). Suppressed when was_cancelling."""
    if was_cancelling:
        return
    started = task.started_at
    if started is not None:
        elapsed = int(time.monotonic() - started)
        label_part = " [%s] · ⏱ %ds" % (task.label, elapsed) if task.label else " · ⏱ %ds" % elapsed
    else:
        label_part = " [%s]" % task.label if task.label else ""
    msg = "✅ Готово ·%s" % label_part
    if nxt is not None:
        msg += "\n▶ дальше: [%s]" % nxt.label
    _tgapi.send(msg, task.reply_to)


def _claude_worker(task: Task) -> None:
    """Execute a claude task, then finish + start next if queued."""
    with _otel.span(
        "tgbot.task.execute",
        **{
            "task.id": task.id,
            "task.label": task.label or "",
            "task.agent": task.agent or "",
            "task.key": task.key,
        },
    ):
        try:
            _tgapi.send("Думаю…" + (" [%s]" % task.label if task.label else ""), task.reply_to)
            out = _cr.run_claude(task.prompt, task.cwd, task.agent, task)
            # If task was cancelled, suppress output
            if task.state != "cancelling":
                _tgapi.send(out, task.reply_to)
        except Exception as e:
            if task.state != "cancelling":
                _tgapi.send("Ошибка: %s" % e, task.reply_to)
        finally:
            was_cancelling = (task.state == "cancelling")
            nxt = _state.finish(task.key)
            _panel.refresh_panel()
            _done_ping(task, nxt, was_cancelling=was_cancelling)
            if nxt is not None:
                _start_worker_thread(nxt)


def dispatch(prompt: str, cwd: str, agent: "str | None", reply_to: "int | None",
             busy_key: str, label: str) -> None:
    """Build a Task, enqueue it, send status reply."""
    task = Task(
        id=_state._new_task_id(),
        key=busy_key,
        label=label,
        prompt=prompt,
        cwd=cwd,
        agent=agent,
        reply_to=reply_to,
        kind="claude",
        state="queued",
        enqueued_at=time.monotonic(),
        started_at=None,
        proc=None,
    )
    started, pos = _state.enqueue(task)
    if started:
        _panel.refresh_panel()
        _start_worker_thread(task)
    else:
        _panel.refresh_panel()
        _tgapi.send("📥 Поставлено в очередь [%s], позиция %d." % (label, pos), reply_to)
