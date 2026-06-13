# tgbridge/app.py — main long-poll loop.
# Assembles all sub-modules and runs the bridge event loop.
import json
import os
import sys
import time

from tgbridge.config import (
    ASK_ANS, ASK_PEND, CHAT, CONTEXTS, DECISION, HELP, HOME, NO, PENDING, YES,
)
from tgbridge.handlers import (
    handle_approval_callback, handle_cancel, handle_queue, handle_text,
)
from tgbridge.media import _media_worker_task, extract_media
import tgbridge.otel as _otel
from tgbridge.panel import (
    get_mode, persistent_reply_keyboard, refresh_panel,
)
from tgbridge.commands import setup_bot_commands
from tgbridge.state import Task, _new_task_id, enqueue
from tgbridge.tgapi import api, api_json, send
from tgbridge.workers import _start_worker_thread


def main():
    _otel.init_tracing()
    for f in (PENDING, DECISION, ASK_PEND, ASK_ANS):
        try:
            os.remove(f)
        except OSError:
            pass

    setup_bot_commands()

    offset = None
    try:
        r = api("getUpdates", {"timeout": 0}, timeout=15)
        if r.get("result"):
            offset = r["result"][-1]["update_id"] + 1
    except Exception:
        pass

    try:
        api_json("sendMessage", {
            "chat_id": CHAT,
            "text": HELP + "\nТекущий режим: " + get_mode(),
            "reply_markup": persistent_reply_keyboard(),
        })
    except Exception as e:
        sys.stderr.write("startup send error: %s\n" % e)

    while True:
        try:
            params = {"timeout": 60, "allowed_updates": json.dumps(["message", "edited_message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset
            resp = api("getUpdates", params, timeout=80)
        except Exception:
            time.sleep(3)
            continue

        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1

            # --- Callback query handling ---
            cq = upd.get("callback_query")
            if cq:
                cchat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                data = cq.get("data", "")
                cq_msg_id = cq.get("message", {}).get("message_id")

                if cchat != CHAT:
                    try:
                        api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
                    except Exception:
                        pass
                    continue

                if data.startswith("approve:"):
                    parts = data.split(":", 2)
                    if len(parts) == 3:
                        _, action, req_id = parts
                        if action in ("allow", "deny"):
                            handle_approval_callback(action, req_id, cq.get("id"), cq_msg_id)
                    else:
                        try:
                            api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
                        except Exception:
                            pass
                    continue

                if data.startswith("cancel:"):
                    try:
                        api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
                    except Exception:
                        pass
                    cancel_arg = data.split(":", 1)[1]
                    handle_cancel(cancel_arg, cq_msg_id)
                    continue

                try:
                    api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
                except Exception:
                    pass
                if data.startswith("mode:"):
                    m = data.split(":", 1)[1]
                    if m in CONTEXTS:
                        from tgbridge.panel import set_mode
                        set_mode(m)
                        refresh_panel()
                        send("✅ Режим переключён: " + m)
                continue

            # --- Message handling ---
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str(msg.get("chat", {}).get("id", ""))
            msg_id = msg.get("message_id")
            text = (msg.get("text") or "").strip()

            if chat != CHAT:
                try:
                    api_json("sendMessage", {"chat_id": chat, "text": "Доступ запрещён."})
                except Exception:
                    pass
                continue

            # Text fallback: да/нет while PENDING exists
            if text:
                low = text.lower()
                if os.path.exists(PENDING) and (low in YES or low in NO):
                    try:
                        with open(PENDING) as f:
                            p = json.load(f)
                    except Exception:
                        pass
                    with open(DECISION, "w") as f:
                        f.write("allow" if low in YES else "deny")
                    try:
                        os.remove(PENDING)
                    except OSError:
                        pass
                    send("✅ Разрешено" if low in YES else "⛔ Запрещено", msg_id)
                    continue

            # ask-IPC
            if text and os.path.exists(ASK_PEND):
                with open(ASK_ANS, "w") as f:
                    f.write(text)
                try:
                    os.remove(ASK_PEND)
                except OSError:
                    pass
                send("✅ Ответ передан агенту.", msg_id)
                continue

            # Media — enqueue via task registry
            media = extract_media(msg)
            if media:
                file_id, filename, mime, kind = media
                media_args = json.dumps([file_id, filename, mime, kind])
                task = Task(
                    id=_new_task_id(),
                    key="media",
                    label="media:%s" % kind,
                    prompt=media_args,
                    cwd=HOME,
                    agent=None,
                    reply_to=msg_id,
                    kind="media",
                    state="queued",
                    enqueued_at=time.monotonic(),
                    started_at=None,
                    proc=None,
                )
                started, pos = enqueue(task)
                if started:
                    refresh_panel()
                    _start_worker_thread(task)
                else:
                    refresh_panel()
                    send("📥 Медиа в очереди, позиция %d." % pos, msg_id)
                continue

            if text:
                handle_text(text, msg_id)
