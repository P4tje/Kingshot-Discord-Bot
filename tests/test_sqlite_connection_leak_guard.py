"""Guard against the FD-exhaustion regression (GitHub #107 class).

`with sqlite3.connect(...) as conn:` does NOT close the connection - it only
commits/rolls back - so a background loop that opens one per iteration leaks a
file descriptor each time until the process hits its RLIMIT_NOFILE and every
new socket/DB open fails ("unable to open database file"). These hot-loop cogs
must wrap every connect in contextlib.closing so the FD is released
deterministically. This catches a bare `with sqlite3.connect(` sneaking back in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

COGS = Path(__file__).resolve().parent.parent / "cogs"

# Cogs with background tasks.loop that reopen connections every cycle. A bare
# `with sqlite3.connect(...)` here is a per-iteration FD leak.
GUARDED = ["alliance_id_channel.py", "bot_backup.py"]

BARE = re.compile(r"with sqlite3\.connect\(")  # `with closing(sqlite3.connect(` won't match


@pytest.mark.parametrize("filename", GUARDED)
def test_no_bare_with_sqlite_connect(filename):
    text = (COGS / filename).read_text(encoding="utf-8")
    offenders = [i for i, line in enumerate(text.splitlines(), 1) if BARE.search(line)]
    assert not offenders, (
        f"{filename} has un-closed `with sqlite3.connect(...)` on line(s) "
        f"{offenders}; wrap in contextlib.closing(...) so the FD is released."
    )
