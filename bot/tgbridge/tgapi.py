# tgbridge/tgapi.py — low-level Telegram API client.
# Depends on tgbridge.config and tgbridge.secrets.
import json
import sys
import urllib.parse
import urllib.request

from tgbridge.config import API, CHAT
from tgbridge.secrets import mask_secrets


def api(method, params=None, timeout=80):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API + "/" + method, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def api_json(method, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(API + "/" + method, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send(text, reply_to=None, reply_markup=None):
    if not text or not text.strip():
        text = "(пустой ответ)"
    # Mask secrets centrally on every outgoing chunk
    text = mask_secrets(text)
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for idx, ch in enumerate(chunks):
        try:
            payload = {"chat_id": CHAT, "text": ch}
            if reply_to and idx == 0:
                payload["reply_to_message_id"] = reply_to
            if reply_markup and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            api_json("sendMessage", payload)
        except Exception as e:
            sys.stderr.write("send error: %s\n" % e)
