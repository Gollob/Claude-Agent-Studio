# tgbridge/secrets.py — secret isolation helpers.
# Depends only on tgbridge.config and stdlib.
import os
import re

from tgbridge.config import (
    TOKEN,
    _CHILD_ENV_ALLOWLIST,
    _CHILD_ENV_ALLOWLIST_PREFIXES,
    _CHILD_ENV_DENYLIST,
)


def child_env() -> "dict[str, str]":
    """Return a filtered environment for child claude processes.

    Uses an allowlist so future secrets are not leaked by default.
    TELEGRAM_* are explicitly excluded.
    """
    result: "dict[str, str]" = {}
    for k, v in os.environ.items():
        if k in _CHILD_ENV_DENYLIST:
            continue
        if k in _CHILD_ENV_ALLOWLIST:
            result[k] = v
            continue
        if any(k.startswith(p) for p in _CHILD_ENV_ALLOWLIST_PREFIXES):
            result[k] = v
    return result


def mask_secrets(text: str) -> str:
    """Remove Telegram token and bot<token>-URL from outgoing text."""
    if not text:
        return text
    # Mask raw token
    text = text.replace(TOKEN, "***")
    # Mask bot<token> URL pattern (e.g. https://api.telegram.org/file/bot<TOKEN>/...)
    text = re.sub(r"bot" + re.escape(TOKEN), "bot***", text)
    # Also mask any occurrence of the token embedded in URLs with different casing
    text = re.sub(r"/bot[A-Za-z0-9_:-]{20,}/", "/bot***/", text)
    return text
