# tgbridge/state.py — SINGLE holder of shared task registry state (ADR-002).
# All modules that read or mutate the registry import from here.
# No other module duplicates lock/running/queues/tasks_by_id.
import collections
import threading
import time
import uuid


class Task:
    """Task object for the registry.

    Plain class (not dataclass) avoids importlib/dataclasses module-lookup
    issues and keeps __slots__ for memory efficiency.
    """
    __slots__ = ("id", "key", "label", "prompt", "cwd", "agent",
                 "reply_to", "kind", "state", "enqueued_at", "started_at", "proc")

    def __init__(self, id: str, key: str, label: str, prompt: str, cwd: str,
                 agent, reply_to, kind: str, state: str,
                 enqueued_at: float, started_at, proc):
        self.id = id              # uuid4 hex[:8]
        self.key = key            # 'dev'|'med'|'sys'|'ag:<name>'|'media'
        self.label = label
        self.prompt = prompt
        self.cwd = cwd
        self.agent = agent        # str | None
        self.reply_to = reply_to  # int | None
        self.kind = kind          # 'claude' | 'media'
        self.state = state        # 'queued'|'running'|'cancelling'|'done'
        self.enqueued_at = enqueued_at
        self.started_at = started_at  # float | None
        self.proc = proc              # subprocess.Popen | None

    def __repr__(self):
        return "Task(id=%r, key=%r, label=%r, state=%r)" % (
            self.id, self.key, self.label, self.state)


# All three registries are under the same lock
lock: threading.Lock = threading.Lock()
running: "dict[str, Task]" = {}                       # key -> active task
queues: "dict[str, collections.deque]" = {}            # key -> FIFO queue
tasks_by_id: "dict[str, Task]" = {}                   # id -> task


def _new_task_id() -> str:
    return uuid.uuid4().hex[:8]


def enqueue(task: Task) -> "tuple[bool, int]":
    """Under lock: start immediately or queue.

    Returns (started, position).  position=0 means started immediately.
    Caller MUST start the worker thread when started=True (outside lock).
    """
    with lock:
        tasks_by_id[task.id] = task
        if task.key not in running:
            task.state = "running"
            task.started_at = time.monotonic()
            running[task.key] = task
            return True, 0
        else:
            if task.key not in queues:
                queues[task.key] = collections.deque()
            queues[task.key].append(task)
            pos = len(queues[task.key])
            return False, pos


def finish(key: str) -> "Task | None":
    """Under lock: remove running task; pop next from queue.

    Returns next Task to start (caller must start its worker thread), or None.
    """
    with lock:
        done_task = running.pop(key, None)
        if done_task is not None:
            done_task.state = "done"
            tasks_by_id.pop(done_task.id, None)
        q = queues.get(key)
        if q:
            nxt = q.popleft()
            if not q:
                del queues[key]
            nxt.state = "running"
            nxt.started_at = time.monotonic()
            running[key] = nxt
            tasks_by_id[nxt.id] = nxt
            return nxt
        return None
