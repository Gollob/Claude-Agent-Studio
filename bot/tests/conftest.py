"""pytest configuration for tg-bot tests.

Ensures that TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are always dummy values
during the test session so no real Telegram API calls can accidentally occur.
Real credentials are never logged.

Also inserts REPO_DIR into sys.path so `import tgbridge` resolves from the
repository root (tgbridge/ package sits next to telegram-bridge.py).

sys.path is set at module level (before any test file imports) so that
tgbridge module-level imports in test files resolve correctly.
"""
import os
import sys
from pathlib import Path
import pytest

# Repo root — one level up from tests/
REPO_DIR = Path(__file__).parent.parent.resolve()

# Set up sys.path at module import time (not just in a fixture) so that
# `import tgbridge.*` at the top of test files works before fixtures run.
_repo_str = str(REPO_DIR)
if _repo_str not in sys.path:
    sys.path.insert(0, _repo_str)

# Pre-load tgbridge package from the dev tree with dummy credentials.
# This caches tgbridge.* in sys.modules BEFORE any test file's module-level
# code can load tg-bot-runtime (via telegram-bridge.py's RUNTIME_DIR logic).
# Without this, test_approval_flow.py (first alphabetically) caches the
# runtime versions, and later test files that import tgbridge directly get
# the wrong (outdated) modules.
_orig_token = os.environ.get("TELEGRAM_TOKEN")
_orig_chat  = os.environ.get("TELEGRAM_CHAT_ID")
os.environ.setdefault("TELEGRAM_TOKEN",   "DUMMY_PRELOAD_TOKEN")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
try:
    # Suppress /tmp/agent makedirs during preload
    import unittest.mock as _mock
    with _mock.patch("os.makedirs"):
        import tgbridge.config   # noqa: F401 — side-effect: caches in sys.modules
except Exception:
    pass  # If it fails, individual tests will handle their own imports


@pytest.fixture(autouse=True, scope="session")
def _setup_sys_path():
    """Ensure tgbridge package is importable from the repo during test session."""
    repo = str(REPO_DIR)
    if repo not in sys.path:
        sys.path.insert(0, repo)


@pytest.fixture(autouse=True, scope="session")
def _force_dummy_telegram_env(_setup_sys_path):
    """Replace Telegram credentials with dummy values for the entire session."""
    orig_token = os.environ.get("TELEGRAM_TOKEN")
    orig_chat  = os.environ.get("TELEGRAM_CHAT_ID")
    os.environ["TELEGRAM_TOKEN"]   = "DUMMY_TOKEN_FOR_TESTS"
    os.environ["TELEGRAM_CHAT_ID"] = "0"
    yield
    # Restore
    if orig_token is None:
        os.environ.pop("TELEGRAM_TOKEN", None)
    else:
        os.environ["TELEGRAM_TOKEN"] = orig_token
    if orig_chat is None:
        os.environ.pop("TELEGRAM_CHAT_ID", None)
    else:
        os.environ["TELEGRAM_CHAT_ID"] = orig_chat
