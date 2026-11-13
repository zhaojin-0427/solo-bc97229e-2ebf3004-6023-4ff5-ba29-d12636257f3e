"""Runtime configuration.

Everything is overridable through environment variables so the same image can
be used for tests, docker-compose and ad-hoc runs.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ARCHIVE_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("ARCHIVE_DB_PATH", str(DATA_DIR / "archive.sqlite3")))

# Hard limits required by the specification / used to bound memory.
QUERY_MIN_CHARS = 1
QUERY_MAX_CHARS = 80
MAX_LINE_BYTES = int(os.environ.get("ARCHIVE_MAX_LINE_BYTES", str(4 * 1024 * 1024)))
INSERT_BATCH = int(os.environ.get("ARCHIVE_INSERT_BATCH", "500"))
SEARCH_LIMIT = 100

# Citation relocation worker. The worker scans jobs in fixed-size batches so
# its memory footprint is independent of the number of citations; the poll
# interval only matters when no work is queued. The thread can be disabled
# (tests drive the batch processor deterministically instead).
RELOCATE_BATCH_SIZE = int(os.environ.get("ARCHIVE_RELOCATE_BATCH", "200"))
RELOCATE_POLL_INTERVAL = float(os.environ.get("ARCHIVE_RELOCATE_POLL", "0.25"))
RELOCATE_THREAD_ENABLED = os.environ.get("ARCHIVE_RELOCATE_THREAD", "1") != "0"
CITATION_PAGE_DEFAULT = 50
CITATION_PAGE_MAX = 200


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
