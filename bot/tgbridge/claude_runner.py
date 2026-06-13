# tgbridge/claude_runner.py — Claude subprocess execution.
# Depends on: config, secrets, process, state, otel.
import subprocess

import tgbridge.otel as _otel
from tgbridge.config import _KILL_GRACE
from tgbridge.secrets import child_env, mask_secrets
from tgbridge.process import _kill_tree
from tgbridge.state import Task, lock


def run_claude(prompt: str, cwd: str, agent: "str | None" = None,
               task: "Task | None" = None) -> str:
    """Run claude subprocess. Store handle in task.proc if provided.

    Uses Popen + communicate(timeout=3600).
    For non-agent calls: tries --continue first, falls back to plain run.
    Returns output string (or error/timeout message).
    """
    env = child_env()
    base = ["claude", "-p", prompt, "--dangerously-skip-permissions",
            "--output-format", "text"]
    if agent:
        base += ["--agent", agent]

    def _spawn(cmd: list) -> "subprocess.Popen":
        return subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def _run_once(cmd: list) -> "tuple[int, str, str]":
        """Spawn, communicate, return (returncode, stdout, stderr)."""
        p = _spawn(cmd)
        # Store proc handle under lock so cancel can always see it
        if task is not None:
            with lock:
                task.proc = p
        try:
            stdout, stderr = p.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            raise
        return p.returncode, stdout or "", stderr or ""

    agent_label = agent or "(default)"
    with _otel.span(
        "tgbot.claude.run",
        **{
            "claude.agent": agent_label,
            "claude.cwd": cwd,
        },
    ) as otel_span:
        try:
            if agent:
                rc, out, err = _run_once(base)
            else:
                # Try --continue first
                try:
                    rc, out, err = _run_once(base + ["--continue"])
                except subprocess.TimeoutExpired:
                    raise  # re-raise to outer handler
                if rc != 0 and not out.strip():
                    # If task was cancelled between the two _run_once calls,
                    # do not start the fallback process.
                    if task is not None and task.state == "cancelling":
                        return ""
                    # Fallback: plain run (no --continue)
                    rc, out, err = _run_once(base)

            out = out.strip()
            if not out:
                tail = mask_secrets((err or "")[-800:])
                out = "(нет вывода)\n" + tail
            try:
                otel_span.set_attribute("claude.output.len", len(out))
                otel_span.set_attribute("claude.returncode", rc)
            except Exception:
                pass
            return out

        except subprocess.TimeoutExpired:
            # proc already killed by _run_once's handler; ensure kill
            if task is not None and task.proc is not None:
                _kill_tree(task.proc)
            return "⏱ Таймаут выполнения (3600с)."
        except Exception as e:
            return "Ошибка claude: %s" % e
