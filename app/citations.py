"""Citation capture and cross-version relocation.

A citation is a researcher's verbatim excerpt anchored to one OCR block of a
specific document version. Two tables separate the immutable capture
(``citations``) from its placement against the *currently active* version
(``citation_locations``):

* ``located``        - block/page/rect/char range are valid for the active
                       version and may be returned to clients;
* ``relocating``     - a background relocation job is still running; no
                       coordinates (not even stale ones) may be served;
* ``pending_review`` - automatic relocation could not decide uniquely; a
                       human must confirm a new range.

Relocation rules after a successful version switch
--------------------------------------------------
1. A block with the same ``block_id`` and *unchanged original text* (compared
   by SHA-256, so old version tables can already have been dropped) keeps its
   original character range verbatim.
2. Otherwise the captured quote is searched verbatim in the new block: it is
   migrated only when it occurs **exactly once**. Missing block, zero or
   multiple occurrences all become ``pending_review`` -- the system never
   guesses a position.

Relocation runs from a durable job queue (``relocation_jobs``) in bounded
batches. All expensive reads and substring scans happen outside any write
transaction; each batch commits one short UPDATE. Every UPDATE is conditional
on the row still belonging to the running job (``state = 'relocating'`` and
the target version still being active), so a late batch from a superseded job
can neither overwrite a newer manual confirmation nor restore old-version
coordinates.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Optional

from . import db

# ---------------------------------------------------------------------------
# Errors (mapped to HTTP by the API layer; under the test shim these are the
# same HTTPException class FastAPI uses).
# ---------------------------------------------------------------------------
from fastapi import HTTPException  # noqa: E402


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count_occurrences(haystack: str, needle: str) -> int:
    """Count overlapping verbatim occurrences of ``needle`` in ``haystack``."""
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return count
        count += 1
        start = idx + 1  # overlapping occurrences count separately


def _get_block(
    conn: sqlite3.Connection, vid: int, block_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"SELECT block_order, block_id, page, text, x0, y0, x1, y1 "
        f"FROM blocks_{vid} WHERE block_id = ?",
        (block_id,),
    ).fetchone()


def _validate_range(block_text: str, char_start: int, char_end: int) -> str:
    if (
        not isinstance(char_start, int)
        or not isinstance(char_end, int)
        or isinstance(char_start, bool)
        or isinstance(char_end, bool)
    ):
        raise HTTPException(
            status_code=400, detail="char_start/char_end must be integers"
        )
    if not (0 <= char_start < char_end <= len(block_text)):
        raise HTTPException(
            status_code=400,
            detail=(
                f"character range [{char_start}, {char_end}) is invalid for "
                f"block text of length {len(block_text)}"
            ),
        )
    quote = block_text[char_start:char_end]
    if not "".join(quote.split()):
        raise HTTPException(
            status_code=400,
            detail="citation range must contain at least one non-whitespace character",
        )
    return quote


# ---------------------------------------------------------------------------
# Citation capture
# ---------------------------------------------------------------------------
def create_citation(
    conn: sqlite3.Connection,
    doc_id: str,
    version_id: int,
    block_id: str,
    char_start: int,
    char_end: int,
) -> dict:
    """Persist a citation captured against ``version_id`` of ``doc_id``.

    The submitted version must be the document's currently active version; a
    stale version is rejected with 409.
    """
    doc = conn.execute(
        "SELECT active_version_id FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if doc is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id!r} not found")
    active_vid = doc["active_version_id"]
    if active_vid is None:
        raise HTTPException(
            status_code=404, detail="document has no active version to cite from"
        )
    if not isinstance(version_id, int) or isinstance(version_id, bool):
        raise HTTPException(status_code=400, detail="version_id must be an integer")
    if version_id != active_vid:
        raise HTTPException(
            status_code=409,
            detail=(
                f"version {version_id} is no longer current; active version is "
                f"{active_vid}; re-locate the excerpt and cite the current version"
            ),
        )

    block = _get_block(conn, active_vid, block_id)
    if block is None:
        raise HTTPException(
            status_code=404,
            detail=f"block_id {block_id!r} not found in version {active_vid}",
        )
    quote = _validate_range(block["text"], char_start, char_end)
    now = time.time()

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO citations(doc_id, created_version_id, block_id, "
            "char_start, char_end, quote, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, active_vid, block_id, char_start, char_end, quote, now),
        )
        citation_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO citation_locations(citation_id, doc_id, "
            "located_version_id, state, block_id, page, char_start, char_end, "
            "quote, block_text_hash, review_reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                citation_id,
                doc_id,
                active_vid,
                db.CITE_LOCATED,
                block_id,
                int(block["page"]),
                char_start,
                char_end,
                quote,
                _hash_text(block["text"]),
                now,
            ),
        )
    view = get_citation(conn, citation_id)
    assert view is not None
    return view


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _load_rows(
    conn: sqlite3.Connection,
    doc_id: Optional[str],
    citation_id: Optional[int],
    status: Optional[str],
    limit: Optional[int],
    offset: int,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.citation_id, c.doc_id, c.created_version_id, "
        "c.block_id AS origin_block_id, l.located_version_id, l.state, "
        "l.block_id AS loc_block_id, l.page AS loc_page, "
        "l.char_start AS loc_char_start, l.char_end AS loc_char_end, "
        "l.quote AS loc_quote, l.block_text_hash, l.review_reason, l.updated_at "
        "FROM citations c JOIN citation_locations l ON l.citation_id = c.citation_id "
    )
    where, params = [], []
    if doc_id is not None:
        where.append("c.doc_id = ?")
        params.append(doc_id)
    if citation_id is not None:
        where.append("c.citation_id = ?")
        params.append(citation_id)
    if status is not None:
        where.append("l.state = ?")
        params.append(status)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY c.citation_id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return list(conn.execute(sql, params))


def _count_rows(
    conn: sqlite3.Connection, doc_id: str, status: Optional[str]
) -> int:
    sql = (
        "SELECT COUNT(*) AS c FROM citations c "
        "JOIN citation_locations l ON l.citation_id = c.citation_id "
        "WHERE c.doc_id = ?"
    )
    params: list = [doc_id]
    if status is not None:
        sql += " AND l.state = ?"
        params.append(status)
    return int(conn.execute(sql, params).fetchone()["c"])


def _build_views(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    if not rows:
        return []
    # Only "located" rows expose coordinates; resolve their blocks in one go.
    # A located row always targets the document's active version.
    located = [r for r in rows if r["state"] == db.CITE_LOCATED]
    block_map: dict[tuple[int, str], sqlite3.Row] = {}
    if located:
        versions = {int(r["located_version_id"]) for r in located}
        for vid in versions:
            ids = [
                r["loc_block_id"]
                for r in located
                if int(r["located_version_id"]) == vid
            ]
            placeholders = ",".join("?" for _ in ids)
            for block in conn.execute(
                f"SELECT block_id, page, x0, y0, x1, y1 FROM blocks_{vid} "
                f"WHERE block_id IN ({placeholders})",
                ids,
            ):
                block_map[(vid, block["block_id"])] = block

    views = []
    for row in rows:
        view = {
            "citation_id": int(row["citation_id"]),
            "doc_id": row["doc_id"],
            "created_version_id": int(row["created_version_id"]),
            "version_id": int(row["located_version_id"]),
            "state": row["state"],
            "quote": row["loc_quote"],
            "block_id": None,
            "page": None,
            "rect": None,
            "char_start": None,
            "char_end": None,
            "review_reason": row["review_reason"],
            "updated_at": float(row["updated_at"]),
        }
        if row["state"] == db.CITE_LOCATED:
            block = block_map.get(
                (int(row["located_version_id"]), row["loc_block_id"])
            )
            if block is not None:  # defensive: invariant says it must exist
                view.update(
                    block_id=row["loc_block_id"],
                    page=int(block["page"]),
                    rect={
                        "x0": block["x0"],
                        "y0": block["y0"],
                        "x1": block["x1"],
                        "y1": block["y1"],
                    },
                    char_start=int(row["loc_char_start"]),
                    char_end=int(row["loc_char_end"]),
                )
        views.append(view)
    return views


def get_citation(conn: sqlite3.Connection, citation_id: int) -> Optional[dict]:
    rows = _load_rows(conn, None, citation_id, None, None, 0)
    views = _build_views(conn, rows)
    return views[0] if views else None


def list_citations(
    conn: sqlite3.Connection,
    doc_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
) -> dict:
    doc = conn.execute(
        "SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if doc is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id!r} not found")
    total = _count_rows(conn, doc_id, status)
    rows = _load_rows(conn, doc_id, None, status, limit, offset)
    items = _build_views(conn, rows)
    return {
        "doc_id": doc_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Manual confirmation of a pending-review citation
# ---------------------------------------------------------------------------
def confirm_citation(
    conn: sqlite3.Connection,
    citation_id: int,
    block_id: str,
    char_start: int,
    char_end: int,
) -> dict:
    row = conn.execute(
        "SELECT l.doc_id, l.state FROM citation_locations l "
        "WHERE l.citation_id = ?",
        (citation_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="citation not found")
    doc_id = row["doc_id"]
    if row["state"] == db.CITE_LOCATED:
        raise HTTPException(
            status_code=409,
            detail="citation is already located against the current version",
        )
    if row["state"] == db.CITE_RELOCATING:
        raise HTTPException(
            status_code=409,
            detail="automatic relocation is still running; retry after it finishes",
        )

    active_vid = db.get_active_version(conn, doc_id)
    if active_vid is None:
        raise HTTPException(status_code=409, detail="document has no active version")
    block = _get_block(conn, active_vid, block_id)
    if block is None:
        raise HTTPException(
            status_code=404,
            detail=f"block_id {block_id!r} not found in version {active_vid}",
        )
    quote = _validate_range(block["text"], char_start, char_end)
    now = time.time()

    # Conditional write: only a citation still awaiting review can be
    # confirmed. A concurrently finished relocation would have moved the row
    # out of pending_review, so the confirmation cannot clobber it.
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE citation_locations SET state = ?, located_version_id = ?, "
            "block_id = ?, page = ?, char_start = ?, char_end = ?, quote = ?, "
            "block_text_hash = ?, review_reason = NULL, updated_at = ? "
            "WHERE citation_id = ? AND state = ?",
            (
                db.CITE_LOCATED,
                active_vid,
                block_id,
                int(block["page"]),
                char_start,
                char_end,
                quote,
                _hash_text(block["text"]),
                now,
                citation_id,
                db.CITE_PENDING_REVIEW,
            ),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail="citation state changed before confirmation; reload and retry",
            )
    view = get_citation(conn, citation_id)
    assert view is not None
    return view


# ---------------------------------------------------------------------------
# Relocation job queue
# ---------------------------------------------------------------------------
def enqueue_relocation(
    conn: sqlite3.Connection,
    doc_id: str,
    from_version_id: Optional[int],
    to_version_id: int,
) -> Optional[int]:
    """Queue a background relocation after ``doc_id`` switched versions.

    All existing locations flip to ``relocating`` in the same short write
    transaction. Documents without citations need no job. Older queued/running
    jobs for the document are superseded.
    """
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM citations WHERE doc_id = ?", (doc_id,)
        ).fetchone()["c"]
    )
    if total == 0:
        return None
    now = time.time()
    with db.transaction(conn):
        conn.execute(
            "UPDATE relocation_jobs SET state = ?, updated_at = ? "
            "WHERE doc_id = ? AND state IN (?, ?)",
            (db.JOB_SUPERSEDED, now, doc_id, db.JOB_PENDING, db.JOB_PROCESSING),
        )
        cur = conn.execute(
            "INSERT INTO relocation_jobs(doc_id, from_version_id, to_version_id, "
            "state, last_citation_id, total_citations, processed, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)",
            (
                doc_id,
                from_version_id,
                to_version_id,
                db.JOB_PENDING,
                total,
                now,
                now,
            ),
        )
        job_id = int(cur.lastrowid)
        # Flip every location to "relocating" and point the version column at
        # the target version, so no old-version metadata can be read while the
        # coordinates themselves are hidden.
        conn.execute(
            "UPDATE citation_locations SET state = ?, located_version_id = ?, "
            "updated_at = ? WHERE doc_id = ?",
            (db.CITE_RELOCATING, to_version_id, now, doc_id),
        )
    return job_id


def recover_jobs(conn: sqlite3.Connection) -> list[str]:
    """Re-arm jobs interrupted by a process restart.

    Citation rows themselves are untouched: a ``relocating`` location stays
    hidden from reads until the resumed job resolves it, so no stale
    coordinates can ever be served.
    """
    messages: list[str] = []
    stuck = conn.execute(
        "SELECT job_id, doc_id FROM relocation_jobs WHERE state = ?",
        (db.JOB_PROCESSING,),
    ).fetchall()
    for row in stuck:
        conn.execute(
            "UPDATE relocation_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
            (db.JOB_PENDING, time.time(), int(row["job_id"])),
        )
        messages.append(
            f"relocation job {row['job_id']} for document {row['doc_id']!r} "
            "resumed after restart"
        )
    conn.commit()
    return messages


def _relocate_one(
    conn: sqlite3.Connection, to_vid: int, loc: sqlite3.Row
) -> dict:
    """Compute the new placement of one citation against version ``to_vid``.

    Returns the fields to write into ``citation_locations``. No writes happen
    here -- the caller batches them into one short transaction.
    """
    block = _get_block(conn, to_vid, loc["block_id"])
    base = {
        "located_version_id": to_vid,
        "updated_at": time.time(),
    }
    if block is None:
        return {
            **base,
            "state": db.CITE_PENDING_REVIEW,
            "block_id": loc["block_id"],
            "page": loc["page"],
            "char_start": loc["char_start"],
            "char_end": loc["char_end"],
            "quote": loc["quote"],
            "block_text_hash": loc["block_text_hash"],
            "review_reason": (
                f"block_missing: block {loc['block_id']!r} does not exist in "
                f"version {to_vid}"
            ),
        }

    new_hash = _hash_text(block["text"])
    if new_hash == loc["block_text_hash"]:
        # Same block id and verbatim-identical original text: the captured
        # range still points at exactly the same characters.
        return {
            **base,
            "state": db.CITE_LOCATED,
            "block_id": block["block_id"],
            "page": int(block["page"]),
            "char_start": int(loc["char_start"]),
            "char_end": int(loc["char_end"]),
            "quote": loc["quote"],
            "block_text_hash": new_hash,
            "review_reason": None,
        }

    occurrences = _count_occurrences(block["text"], loc["quote"])
    if occurrences == 1:
        new_start = block["text"].find(loc["quote"])
        return {
            **base,
            "state": db.CITE_LOCATED,
            "block_id": block["block_id"],
            "page": int(block["page"]),
            "char_start": new_start,
            "char_end": new_start + len(loc["quote"]),
            "quote": loc["quote"],
            "block_text_hash": new_hash,
            "review_reason": None,
        }

    reason = (
        f"quote_not_found: excerpt absent from block {block['block_id']!r} "
        f"in version {to_vid}"
        if occurrences == 0
        else
        f"quote_ambiguous: excerpt occurs {occurrences} times in block "
        f"{block['block_id']!r} of version {to_vid}; human review required"
    )
    # Keep the last-known placement in storage for auditor context, but the
    # pending_review state makes the API hide every coordinate.
    return {
        **base,
        "state": db.CITE_PENDING_REVIEW,
        "block_id": loc["block_id"],
        "page": loc["page"],
        "char_start": loc["char_start"],
        "char_end": loc["char_end"],
        "quote": loc["quote"],
        "block_text_hash": loc["block_text_hash"],
        "review_reason": reason,
    }


def process_job_batch(
    conn: sqlite3.Connection, job_id: int, batch_size: int
) -> bool:
    """Process one bounded batch of a relocation job.

    Returns ``True`` when the job has reached a terminal state. Reads and the
    substring scans run without any open write transaction; only the final
    batch UPDATE takes a short write lock, so relocation never occupies a
    business-database write transaction while computing.
    """
    job = conn.execute(
        "SELECT * FROM relocation_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if job is None or job["state"] in (db.JOB_DONE, db.JOB_SUPERSEDED):
        return True

    active_vid = db.get_active_version(conn, job["doc_id"])
    if active_vid != int(job["to_version_id"]):
        # A newer version has been activated since this job was queued; any
        # late write from this job must not restore old-version placements.
        with db.transaction(conn):
            conn.execute(
                "UPDATE relocation_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (db.JOB_SUPERSEDED, time.time(), job_id),
            )
        return True

    to_vid = int(job["to_version_id"])
    last_id = int(job["last_citation_id"])

    # Read-only snapshot for the whole batch computation.
    conn.execute("BEGIN DEFERRED")
    batch = conn.execute(
        "SELECT * FROM citation_locations WHERE doc_id = ? AND state = ? "
        "AND citation_id > ? ORDER BY citation_id LIMIT ?",
        (job["doc_id"], db.CITE_RELOCATING, last_id, batch_size),
    ).fetchall()
    decisions = [_relocate_one(conn, to_vid, loc) for loc in batch]
    conn.rollback()  # read-only snapshot: nothing to commit

    if not batch:
        with db.transaction(conn):
            conn.execute(
                "UPDATE relocation_jobs SET state = ?, processed = total_citations, "
                "updated_at = ? WHERE job_id = ?",
                (db.JOB_DONE, time.time(), job_id),
            )
        return True

    applied = 0
    max_id = last_id
    with db.transaction(conn):
        for loc, decision in zip(batch, decisions):
            cur = conn.execute(
                "UPDATE citation_locations SET state = ?, located_version_id = ?, "
                "block_id = ?, page = ?, char_start = ?, char_end = ?, quote = ?, "
                "block_text_hash = ?, review_reason = ?, updated_at = ? "
                "WHERE citation_id = ? AND state = ?",
                (
                    decision["state"],
                    decision["located_version_id"],
                    decision["block_id"],
                    decision["page"],
                    decision["char_start"],
                    decision["char_end"],
                    decision["quote"],
                    decision["block_text_hash"],
                    decision["review_reason"],
                    decision["updated_at"],
                    int(loc["citation_id"]),
                    db.CITE_RELOCATING,
                ),
            )
            applied += cur.rowcount
            max_id = max(max_id, int(loc["citation_id"]))

        finished = len(batch) < batch_size
        conn.execute(
            "UPDATE relocation_jobs SET state = ?, last_citation_id = ?, "
            "processed = processed + ?, updated_at = ? WHERE job_id = ?",
            (
                db.JOB_DONE if finished else db.JOB_PENDING,
                max_id,
                applied,
                time.time(),
                job_id,
            ),
        )
    return finished


def claim_next_job(conn: sqlite3.Connection) -> Optional[int]:
    """Return the oldest open job id, atomically marking it ``processing``."""
    row = conn.execute(
        "SELECT job_id FROM relocation_jobs WHERE state IN (?, ?) "
        "ORDER BY job_id LIMIT 1",
        (db.JOB_PENDING, db.JOB_PROCESSING),
    ).fetchone()
    if row is None:
        return None
    job_id = int(row["job_id"])
    conn.execute(
        "UPDATE relocation_jobs SET state = ?, updated_at = ? WHERE job_id = ? "
        "AND state IN (?, ?)",
        (db.JOB_PROCESSING, time.time(), job_id, db.JOB_PENDING, db.JOB_PROCESSING),
    )
    conn.commit()
    return job_id
