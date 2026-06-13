# tgbridge/media.py — media file handling and worker.
# Depends on: config, tgapi, secrets, state, claude_runner, panel, otel.
import http.client
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import tgbridge.otel as _otel
from tgbridge.config import TOKEN, CONTEXTS, FILE_INTAKE_URL
from tgbridge.tgapi import api, send
from tgbridge.secrets import mask_secrets
from tgbridge.state import Task, finish
from tgbridge.claude_runner import run_claude
from tgbridge.panel import get_mode, refresh_panel


def get_file_url(file_id):
    r = api("getFile", {"file_id": file_id}, timeout=15)
    path = r["result"]["file_path"]
    # Build URL but never expose it in messages/logs
    url = "https://api.telegram.org/file/bot%s/%s" % (TOKEN, path)
    return url, path


def download_tg_file(file_id):
    url, fpath = get_file_url(file_id)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(), Path(fpath).name, Path(fpath).suffix
    except Exception as e:
        raise Exception(mask_secrets(str(e))) from None


def call_file_intake(data, filename, mime):
    boundary = "----TGBridgeBoundary"
    body_parts = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'
    ).encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    host = FILE_INTAKE_URL.replace("http://", "").split("/")[0]
    host_part, port_part = (host.split(":") + ["8090"])[:2]
    conn = http.client.HTTPConnection(host_part, int(port_part), timeout=300)
    conn.request("POST", "/process", body=body_parts,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = conn.getresponse()
    result = json.loads(resp.read())
    return result.get("text", "Нет ответа")


def save_media_temp(data, filename):
    """Write data to /tmp/agent/intake/<timestamp>_<safe-name>. Return full path."""
    intake_dir = "/tmp/agent/intake"
    os.makedirs(intake_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "file"))
    safe = safe[:80] or "file"
    name = "%d_%s" % (int(time.time()), safe)
    path = os.path.join(intake_dir, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def build_file_prompt(path, kind, mime, mode):
    """Return a Russian instruction for the agent to read and analyse the file."""
    text = (
        "Пользователь прислал файл через Telegram: %s (тип: %s, вид: %s). "
        "Прочитай его инструментом Read и извлеки содержимое: "
        "для фото/скана — распознай весь текст и кратко структурируй; "
        "для PDF — извлеки ключевое. Отвечай по-русски, по делу." % (path, mime, kind)
    )
    if mode == "med":
        text += (
            " Если это медицинский документ (анализы, выписка, снимок) — "
            "выдели показатели/диагнозы/назначения и при уместности предложи занести в медкарту."
        )
    return text


def _media_worker_task(task: Task, file_id: str, filename: str,
                       mime: str, kind: str) -> None:
    """Media worker driven by task registry."""
    # Import here to avoid circular import (workers imports media and media imports workers)
    from tgbridge.workers import _start_worker_thread

    tmp_path = None
    with _otel.span(
        "tgbot.media.process",
        **{
            "media.kind": kind,
            "media.mime": mime,
            "task.id": task.id,
            "task.label": task.label or "",
        },
    ):
        try:
            send("⏳ Скачиваю и обрабатываю…", task.reply_to)
            data, fname, _ = download_tg_file(file_id)
            if kind in ("voice", "audio"):
                text = call_file_intake(data, filename or fname, mime)
                mode = get_mode()
                send("🎙→ %s\n[режим: %s]" % (text[:300], mode), task.reply_to)
                if task.state != "cancelling":
                    send(run_claude(text, CONTEXTS[mode], None, task), task.reply_to)
            else:
                tmp_path = save_media_temp(data, filename or fname)
                mode = get_mode()
                agent = None
                send("🖼→ читаю %s [режим: %s]…" % (kind, mode), task.reply_to)
                if task.state != "cancelling":
                    out = run_claude(build_file_prompt(tmp_path, kind, mime, mode),
                                     CONTEXTS[mode], agent, task)
                    for i in range(0, len(out), 3800):
                        send(out[i:i+3800], task.reply_to)
        except Exception as e:
            send("❌ Ошибка обработки: %s" % mask_secrets(str(e)), task.reply_to)
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            was_cancelling = (task.state == "cancelling")
            nxt = finish(task.key)
            refresh_panel()
            from tgbridge.workers import _done_ping
            _done_ping(task, nxt, was_cancelling=was_cancelling)
            if nxt is not None:
                _start_worker_thread(nxt)


def extract_media(msg):
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return best["file_id"], "photo.jpg", "image/jpeg", "photo"
    if msg.get("document"):
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name", "document"), doc.get("mime_type", "application/octet-stream"), "document"
    if msg.get("voice"):
        return msg["voice"]["file_id"], "voice.ogg", "audio/ogg", "voice"
    if msg.get("audio"):
        a = msg["audio"]
        return a["file_id"], a.get("file_name", "audio.mp3"), a.get("mime_type", "audio/mpeg"), "audio"
    return None
