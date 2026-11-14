import os

os.environ.setdefault("XQO_ATTENTION_TIMEOUT", "0.2")  # auto-confirm quickly in tests

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

# test_peers_sdk reuses the fake Claude Agent SDK from the base package's tests (same monorepo)
BASE_TESTS = Path(__file__).resolve().parents[2] / "xqo" / "tests"
if BASE_TESTS.is_dir():
    sys.path.insert(0, str(BASE_TESTS))
