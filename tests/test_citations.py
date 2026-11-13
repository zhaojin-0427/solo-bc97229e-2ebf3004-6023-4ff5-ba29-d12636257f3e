"""Tests for citation capture and cross-version relocation.

The relocation worker thread is disabled in this harness (see conftest); jobs
are drained one batch at a time via :func:`drain_jobs`, which makes every
ordering and stale-write scenario deterministic.
"""
from __future__ import annotations

import json
import time
import tracemalloc
import unittest

from app import citations, config, db
from app.db import get_conn

from tests.helpers import block, make_app, ndjson, reset_db, wsgi_request


def upload(app, doc_id: str, records: list[dict]) -> int:
    status, body = wsgi_request(
        app, "PUT", f"/documents/{doc_id}", ndjson(records)
    )
    assert status == 202, body
    return int(body["version_id"])


def drain_jobs(max_iterations: int = 1000) -> None:
    """Process every open relocation job to completion, batch by batch."""
    conn = get_conn()
    try:
        for _ in range(max_iterations):
            job_id = citations.claim_next_job(conn)
            if job_id is None:
                return
            done = False
            while not done:
                done = citations.process_job_batch(
                    conn, job_id, config.RELOCATE_BATCH_SIZE
                )
        raise AssertionError("relocation jobs did not converge")
    finally:
        conn.close()


def post_json(app, path: str, payload: dict) -> tuple[int, dict]:
    return wsgi_request(
        app, "POST", path, json.dumps(payload).encode("utf-8")
    )


def doc_version(app, doc_id: str) -> int:
    status, body = wsgi_request(app, "GET", f"/documents/{doc_id}/versions")
    assert status == 200
    ready = [v for v in body if v["state"] == "ready"]
    return max(v["version_id"] for v in ready)


class CitationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_db()
        self.app = make_app()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def test_create_citation_returns_id_and_current_coordinates(self):
        vid = upload(self.app, "doc-1", [block("b1", 1, "Hello world"),
                                         block("b2", 2, "foo bar")])
        status, body = post_json(
            self.app,
            "/documents/doc-1/citations",
            {"version_id": vid, "block_id": "b1",
             "char_start": 6, "char_end": 11},
        )
        self.assertEqual(status, 201, body)
        self.assertIn("citation_id", body)
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["quote"], "world")
        self.assertEqual(body["block_id"], "b1")
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["char_start"], 6)
        self.assertEqual(body["char_end"], 11)
        self.assertEqual(body["rect"], {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0})
        self.assertEqual(body["version_id"], vid)
        self.assertEqual(body["created_version_id"], vid)

        # Single-citation fetch.
        cid = body["citation_id"]
        status, again = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(again["quote"], "world")

    def test_stale_version_rejected_with_409(self):
        old_vid = upload(self.app, "doc-stale", [block("b1", 1, "version one text")])
        new_vid = upload(self.app, "doc-stale", [block("b1", 1, "version two text")])
        self.assertNotEqual(old_vid, new_vid)
        drain_jobs()

        status, body = post_json(
            self.app,
            "/documents/doc-stale/citations",
            {"version_id": old_vid, "block_id": "b1", "char_start": 0, "char_end": 3},
        )
        self.assertEqual(status, 409, body)
        self.assertIn(str(new_vid), body["detail"])

        # The current version is accepted.
        status, body = post_json(
            self.app,
            "/documents/doc-stale/citations",
            {"version_id": new_vid, "block_id": "b1", "char_start": 0, "char_end": 3},
        )
        self.assertEqual(status, 201, body)

    def test_citation_validation_errors(self):
        vid = upload(self.app, "doc-val", [block("b1", 1, "abcdef")])
        # unknown document
        status, body = post_json(
            self.app,
            "/documents/ghost/citations",
            {"version_id": vid, "block_id": "b1", "char_start": 0, "char_end": 1},
        )
        self.assertEqual(status, 404)
        # unknown block
        status, body = post_json(
            self.app,
            "/documents/doc-val/citations",
            {"version_id": vid, "block_id": "nope", "char_start": 0, "char_end": 1},
        )
        self.assertEqual(status, 404)
        # inverted / out-of-range ranges
        for bad_range in ((2, 2), (3, 1), (-1, 1), (0, 7)):
            status, body = post_json(
                self.app,
                "/documents/doc-val/citations",
                {"version_id": vid, "block_id": "b1",
                 "char_start": bad_range[0], "char_end": bad_range[1]},
            )
            self.assertEqual(status, 400, bad_range)
        # whitespace-only range
        vid2 = upload(self.app, "doc-ws2", [block("b1", 1, "ab  cd")])
        status, body = post_json(
            self.app,
            "/documents/doc-ws2/citations",
            {"version_id": vid2, "block_id": "b1", "char_start": 2, "char_end": 4},
        )
        self.assertEqual(status, 400)
        # malformed JSON body
        status, body = wsgi_request(
            self.app, "POST", "/documents/doc-val/citations", b"{not json"
        )
        self.assertEqual(status, 400)

    # ------------------------------------------------------------------
    # Relocation: same-id block, unchanged text -> range carried over
    # ------------------------------------------------------------------
    def test_unchanged_block_keeps_range(self):
        vid1 = upload(self.app, "doc-rel", [
            block("b1", 1, "alpha bravo charlie"),
            block("b2", 1, "delta echo"),
        ])
        status, cite = post_json(
            self.app,
            "/documents/doc-rel/citations",
            {"version_id": vid1, "block_id": "b2", "char_start": 6, "char_end": 10},
        )
        self.assertEqual(status, 201)
        self.assertEqual(cite["quote"], "echo")
        cid = cite["citation_id"]

        # New version: b2 keeps identical text (and rect), b1 changes.
        vid2 = upload(self.app, "doc-rel", [
            block("b1", 1, "alpha bravo CHARLIE-X"),
            block("b2", 1, "delta echo"),
        ])
        # Before the worker runs, no stale coordinates may be served.
        status, page = wsgi_request(
            self.app, "GET", "/documents/doc-rel/citations"
        )
        self.assertEqual(page["items"][0]["state"], "relocating")
        self.assertIsNone(page["items"][0]["block_id"])
        self.assertIsNone(page["items"][0]["rect"])
        self.assertIsNone(page["items"][0]["char_start"])
        self.assertEqual(page["items"][0]["quote"], "echo")

        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["version_id"], vid2)
        self.assertEqual(body["block_id"], "b2")
        self.assertEqual(body["char_start"], 6)
        self.assertEqual(body["char_end"], 10)
        self.assertEqual(body["quote"], "echo")
        self.assertEqual(body["page"], 1)

    # ------------------------------------------------------------------
    # Relocation: changed text, unique verbatim occurrence -> migrated
    # ------------------------------------------------------------------
    def test_changed_block_unique_occurrence_migrates(self):
        vid1 = upload(self.app, "doc-mig", [
            block("b1", 3, "prefix 中間 suffix"),
        ])
        status, cite = post_json(
            self.app,
            "/documents/doc-mig/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 7, "char_end": 9},
        )
        self.assertEqual(cite["quote"], "中間")
        cid = cite["citation_id"]

        vid2 = upload(self.app, "doc-mig", [
            block("b1", 3, "全新prefix 中間 後綴suffix"),
        ])
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["char_start"], 9)
        self.assertEqual(body["char_end"], 11)
        self.assertEqual(body["block_id"], "b1")
        self.assertEqual(body["quote"], "中間")
        # Range slices the exact quote out of the new original text.
        conn = get_conn()
        try:
            row = conn.execute(
                f"SELECT text FROM blocks_{vid2} WHERE block_id = 'b1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            row["text"][body["char_start"]:body["char_end"]], "中間"
        )

    # ------------------------------------------------------------------
    # Relocation: missing block / zero / multiple occurrences -> review
    # ------------------------------------------------------------------
    def test_missing_zero_and_multiple_occurrences_become_pending_review(self):
        vid1 = upload(self.app, "doc-rev", [
            block("keep", 1, "hello world"),
            block("gone", 1, "vanish me"),
            block("dup", 1, "only xray once"),
            block("lost", 1, "nowhere phrase"),
        ])
        cases = [
            ("keep", 0, 5, "hello"),   # text changes; quote present twice
            ("gone", 0, 6, "vanish"),  # block disappears
            ("dup", 5, 9, "xray"),     # text changes; quote disappears
            ("lost", 8, 14, "phrase"), # text changes; quote present once -> located
        ]
        cids = {}
        for bid, s, e, quote in cases:
            status, cite = post_json(
                self.app,
                "/documents/doc-rev/citations",
                {"version_id": vid1, "block_id": bid, "char_start": s, "char_end": e},
            )
            self.assertEqual(status, 201, (bid, cite))
            cids[bid] = cite["citation_id"]

        upload(self.app, "doc-rev", [
            block("keep", 1, "hello to hello again"),       # "hello" x2
            block("dup", 1, "different content entirely"),  # no xray
            block("lost", 2, "phrase moved pages"),         # unique phrase
        ])
        drain_jobs()

        status, body = wsgi_request(self.app, "GET", f"/citations/{cids['keep']}")
        self.assertEqual(body["state"], "pending_review")
        self.assertIn("quote_ambiguous", body["review_reason"])
        self.assertIsNone(body["rect"])
        self.assertIsNone(body["page"])

        status, body = wsgi_request(self.app, "GET", f"/citations/{cids['gone']}")
        self.assertEqual(body["state"], "pending_review")
        self.assertIn("block_missing", body["review_reason"])

        status, body = wsgi_request(self.app, "GET", f"/citations/{cids['dup']}")
        self.assertEqual(body["state"], "pending_review")
        self.assertIn("quote_not_found", body["review_reason"])

        status, body = wsgi_request(self.app, "GET", f"/citations/{cids['lost']}")
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["char_start"], 0)

        # Status filter returns only the review queue.
        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rev/citations",
            query={"status": "pending_review"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 3)
        self.assertEqual({i["citation_id"] for i in page["items"]},
                         {cids["keep"], cids["gone"], cids["dup"]})
        bad = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rev/citations",
            query={"status": "bogus"},
        )
        self.assertEqual(bad[0], 400)
    def test_overlapping_occurrences_count_separately(self):
        # "aaa" occurs twice (overlapping) in "aaaa" -> ambiguous, no guessing.
        vid1 = upload(self.app, "doc-ov", [block("b1", 1, "aaa tail")])
        status, cite = post_json(
            self.app,
            "/documents/doc-ov/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 0, "char_end": 3},
        )
        cid = cite["citation_id"]
        upload(self.app, "doc-ov", [block("b1", 1, "aaaa head")])
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "pending_review")
        self.assertIn("2 times", body["review_reason"])

    # ------------------------------------------------------------------
    # Manual confirmation
    # ------------------------------------------------------------------
    def test_manual_confirmation_updates_quote_and_locates(self):
        vid1 = upload(self.app, "doc-conf", [block("b1", 1, "old sentence text")])
        status, cite = post_json(
            self.app,
            "/documents/doc-conf/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 0, "char_end": 3},
        )
        cid = cite["citation_id"]
        vid2 = upload(self.app, "doc-conf", [
            block("b1", 1, "new sentence text"),
            block("b2", 1, "moved old part here"),
        ])
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "pending_review")

        # Out-of-range range is rejected while pending.
        status, bad = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b2", "char_start": 6, "char_end": 99},
        )
        self.assertEqual(status, 400, bad)
        # Unknown block is rejected too.
        status, bad = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "ghost", "char_start": 0, "char_end": 1},
        )
        self.assertEqual(status, 404, bad)
        # Confirm a valid range in the current version, including a different block.
        status, body = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b2", "char_start": 6, "char_end": 15},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["version_id"], vid2)
        self.assertEqual(body["block_id"], "b2")
        self.assertEqual(body["quote"], "old part ")
        self.assertIsNone(body["review_reason"])

        # Confirming an already-located citation is a conflict.
        status, again = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b2", "char_start": 0, "char_end": 5},
        )
        self.assertEqual(status, 409)

    def test_confirmation_blocked_while_relocating_and_unknown_citation_404(self):
        vid1 = upload(self.app, "doc-busy", [block("b1", 1, "alpha beta")])
        status, cite = post_json(
            self.app,
            "/documents/doc-busy/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 0, "char_end": 5},
        )
        cid = cite["citation_id"]
        upload(self.app, "doc-busy", [block("b1", 1, "alpha BETA changed")])
        # No drain: relocation is still pending.
        status, body = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b1", "char_start": 0, "char_end": 5},
        )
        self.assertEqual(status, 409)
        self.assertIn("relocation", body["detail"])

        status, _ = wsgi_request(self.app, "GET", "/citations/999999")
        self.assertEqual(status, 404)
        status, _ = post_json(
            self.app,
            "/citations/999999/confirm",
            {"block_id": "b1", "char_start": 0, "char_end": 1},
        )
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------
    # Concurrency: slow relocation vs. repeated switches / confirmations
    # ------------------------------------------------------------------
    def test_late_batch_cannot_overwrite_confirmation_or_old_coordinates(self):
        vid1 = upload(self.app, "doc-race", [block("b1", 1, "cat dog bird")])
        status, cite = post_json(
            self.app,
            "/documents/doc-race/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 0, "char_end": 3},
        )
        cid = cite["citation_id"]

        # Switch to v2 and leave its job half-processed (simulate a slow job).
        vid2 = upload(self.app, "doc-race", [block("b1", 1, "cat! dog! bird!")])
        conn = get_conn()
        try:
            job2 = citations.claim_next_job(conn)
            self.assertIsNotNone(job2)
            # v3 arrives before job2 finishes; job2 is superseded on enqueue.
            # The quote "cat" will occur twice in v3, so the v3 relocation
            # itself must end in pending_review (no guessing).
        finally:
            conn.close()
        vid3 = upload(self.app, "doc-race", [block("b1", 1, "cat!! and cat again")])

        conn = get_conn()
        try:
            # A late batch from the stale v2 job must no-op / be rejected.
            late_done = citations.process_job_batch(conn, job2, 10)
            self.assertTrue(late_done)
            job_row = conn.execute(
                "SELECT state FROM relocation_jobs WHERE job_id = ?", (job2,)
            ).fetchone()
            self.assertEqual(job_row["state"], "superseded")
            loc = conn.execute(
                "SELECT state, located_version_id FROM citation_locations "
                "WHERE citation_id = ?",
                (cid,),
            ).fetchone()
            # No old (v1/v2) coordinates restored.
            self.assertEqual(loc["state"], "relocating")
            self.assertNotIn(loc["located_version_id"], (vid1, vid2))
        finally:
            conn.close()

        # Human confirms while the v3 job is still open.
        status, body = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b1", "char_start": 0, "char_end": 5},
        )
        self.assertEqual(status, 409)  # still relocating -> must wait
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "pending_review")
        status, confirmed = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b1", "char_start": 0, "char_end": 5},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["quote"], "cat!!")

        # Now run any residual stale batches again: the conditional UPDATE
        # must not overwrite the human confirmation.
        conn = get_conn()
        try:
            citations.process_job_batch(conn, job2, 10)
            loc = conn.execute(
                "SELECT state, located_version_id, quote, char_start, char_end "
                "FROM citation_locations WHERE citation_id = ?",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(loc["state"], "located")
        self.assertEqual(loc["located_version_id"], vid3)
        self.assertEqual(loc["quote"], "cat!!")
        self.assertEqual((loc["char_start"], loc["char_end"]), (0, 5))

    def test_confirmation_then_switch_starts_fresh_relocation(self):
        vid1 = upload(self.app, "doc-cs", [block("b1", 1, "aaa bbb")])
        status, cite = post_json(
            self.app,
            "/documents/doc-cs/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 4, "char_end": 7},
        )
        cid = cite["citation_id"]
        vid2 = upload(self.app, "doc-cs", [block("b1", 1, "aaa BBB")])
        drain_jobs()
        status, body = post_json(
            self.app,
            f"/citations/{cid}/confirm",
            {"block_id": "b1", "char_start": 4, "char_end": 7},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["quote"], "BBB")
        # Another switch relocates from the confirmed placement.
        vid3 = upload(self.app, "doc-cs", [block("b1", 1, "aaa BBB ccc")])
        status, page = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(page["state"], "relocating")
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["version_id"], vid3)
        self.assertEqual(body["quote"], "BBB")
        self.assertEqual((body["char_start"], body["char_end"]), (4, 7))

    # ------------------------------------------------------------------
    # Restart / failure semantics
    # ------------------------------------------------------------------
    def test_restart_resumes_unfinished_relocation(self):
        vid1 = upload(self.app, "doc-restart", [block("b1", 1, "migrate me please")])
        post_json(
            self.app,
            "/documents/doc-restart/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 8, "char_end": 10},
        )
        vid2 = upload(self.app, "doc-restart", [block("b1", 1, "xx migrate me please")])

        conn = get_conn()
        try:
            job_id = citations.claim_next_job(conn)
            self.assertIsNotNone(job_id)
            # Simulate the process dying mid-batch: job stays "processing".
            row = conn.execute(
                "SELECT state FROM relocation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            self.assertEqual(row["state"], "processing")
        finally:
            conn.close()

        # New app instance: startup recovery runs with the first request
        # (the shim, like uvicorn, enters the lifespan lazily/at startup).
        app2 = make_app()
        wsgi_request(app2, "GET", "/health")
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT state FROM relocation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            self.assertEqual(row["state"], "pending")
        finally:
            conn.close()
        drain_jobs()
        status, page = wsgi_request(
            app2, "GET", "/documents/doc-restart/citations"
        )
        self.assertEqual(page["items"][0]["state"], "located")
        self.assertEqual(page["items"][0]["version_id"], vid2)
        status, report = wsgi_request(app2, "GET", "/recovery")
        self.assertTrue(
            any("relocation job" in m and "doc-restart" in m
                for m in report["recovered"]),
            report,
        )

    def test_failed_replacement_does_not_touch_citations(self):
        vid1 = upload(self.app, "doc-fail", [block("b1", 1, "stable citation text")])
        status, cite = post_json(
            self.app,
            "/documents/doc-fail/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 7, "char_end": 15},
        )
        cid = cite["citation_id"]
        bad = (json.dumps(block("b1", 1, "would be new")) + "\n{broken").encode()
        status, body = wsgi_request(self.app, "PUT", "/documents/doc-fail", bad)
        self.assertEqual(status, 400)
        # No relocation job was created, citation stays located on the old version.
        status, jobs = wsgi_request(
            self.app, "GET", "/documents/doc-fail/relocation-jobs"
        )
        self.assertEqual(jobs, [])
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["version_id"], vid1)
        self.assertEqual(body["quote"], "citation")

    # ------------------------------------------------------------------
    # Pagination and bounded memory
    # ------------------------------------------------------------------
    def test_pagination_order_and_has_more(self):
        vid = upload(self.app, "doc-page", [block("b1", 1, "abcdefghij")])
        for i in range(5):
            status, body = post_json(
                self.app,
                "/documents/doc-page/citations",
                {"version_id": vid, "block_id": "b1",
                 "char_start": i, "char_end": i + 1},
            )
            self.assertEqual(status, 201)
        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-page/citations",
            query={"limit": 2, "offset": 0},
        )
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        ids_first = [i["citation_id"] for i in page["items"]]
        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-page/citations",
            query={"limit": 2, "offset": 4},
        )
        self.assertEqual(len(page["items"]), 1)
        self.assertFalse(page["has_more"])
        status, bad = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-page/citations",
            query={"limit": 0},
        )
        self.assertEqual(status, 400)
        status, bad = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-page/citations",
            query={"offset": -1},
        )
        self.assertEqual(status, 400)
        # ids are globally increasing
        self.assertEqual(ids_first, sorted(ids_first))

    def test_relocation_memory_does_not_grow_with_citation_count(self):
        n = 4000
        records = [block(f"b{i:05d}", 1, f"text-{i} 保留段") for i in range(n)]
        vid1 = upload(self.app, "doc-big-cite", records)
        conn = get_conn()
        try:
            now = time.time()
            with db.transaction(conn):
                conn.executemany(
                    "INSERT INTO citations(doc_id, created_version_id, block_id, "
                    "char_start, char_end, quote, created_at) "
                    "VALUES ('doc-big-cite', ?, ?, 8, 11, '保留段', ?)",
                    [(vid1, f"b{i:05d}", now) for i in range(n)],
                )
                conn.execute(
                    "INSERT INTO citation_locations(citation_id, doc_id, "
                    "located_version_id, state, block_id, page, char_start, "
                    "char_end, quote, block_text_hash, updated_at) "
                    "SELECT citation_id, 'doc-big-cite', ?, 'located', block_id, "
                    "1, 8, 11, '保留段', ?, ? FROM citations "
                    "WHERE doc_id = 'doc-big-cite'",
                    (vid1, "x", now),
                )
        finally:
            conn.close()

        # New version: every block changes, but keeps the unique quote.
        new_records = [
            block(f"b{i:05d}", 1, f"new-text-{i} 保留段 tail") for i in range(n)
        ]
        vid2 = upload(self.app, "doc-big-cite", new_records)
        conn = get_conn()
        try:
            job_id = citations.claim_next_job(conn)
            self.assertIsNotNone(job_id)
            tracemalloc.start()
            iterations = 0
            done = False
            while not done:
                done = citations.process_job_batch(conn, job_id, 200)
                iterations += 1
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        finally:
            conn.close()

        self.assertGreater(iterations, n / 200)  # genuinely batched
        # 4000 citations, but one batch (200 rows) is the largest live object set.
        self.assertLess(peak, 2 * 1024 * 1024, f"peak={peak}")

        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-big-cite/citations",
            query={"status": "located", "limit": 1},
        )
        self.assertEqual(page["total"], n)
        status, review = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-big-cite/citations",
            query={"status": "pending_review", "limit": 1},
        )
        self.assertEqual(review["total"], 0)
        # And the migrated ranges actually slice the new text.
        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-big-cite/citations",
            query={"limit": 3},
        )
        conn = get_conn()
        try:
            for item in page["items"]:
                row = conn.execute(
                    f"SELECT text FROM blocks_{vid2} WHERE block_id = ?",
                    (item["block_id"],),
                ).fetchone()
                self.assertEqual(
                    row["text"][item["char_start"]:item["char_end"]],
                    "保留段",
                )
        finally:
            conn.close()

    def test_citation_created_during_open_job_is_not_relocated_or_regressed(self):
        vid1 = upload(self.app, "doc-mid", [block("b1", 1, "alpha beta")])
        status, cite = post_json(
            self.app,
            "/documents/doc-mid/citations",
            {"version_id": vid1, "block_id": "b1", "char_start": 0, "char_end": 5},
        )
        cid1 = cite["citation_id"]
        vid2 = upload(self.app, "doc-mid", [block("b1", 1, "alpha BETA")])
        # Job is queued but not drained; capture a fresh citation on v2.
        status, fresh = post_json(
            self.app,
            "/documents/doc-mid/citations",
            {"version_id": vid2, "block_id": "b1", "char_start": 6, "char_end": 10},
        )
        self.assertEqual(status, 201)
        cid2 = fresh["citation_id"]
        self.assertEqual(fresh["state"], "located")
        self.assertEqual(fresh["quote"], "BETA")

        drain_jobs()
        status, one = wsgi_request(self.app, "GET", f"/citations/{cid1}")
        self.assertEqual(one["state"], "located")
        self.assertEqual(one["version_id"], vid2)
        status, two = wsgi_request(self.app, "GET", f"/citations/{cid2}")
        # The fresh citation must keep its exact placement, untouched by the job.
        self.assertEqual(two["state"], "located")
        self.assertEqual(two["version_id"], vid2)
        self.assertEqual(two["quote"], "BETA")
        self.assertEqual((two["char_start"], two["char_end"]), (6, 10))

    def test_no_citations_switch_creates_no_job(self):
        upload(self.app, "doc-empty-cite", [block("b1", 1, "nothing cited here")])
        upload(self.app, "doc-empty-cite", [block("b1", 1, "still nothing cited")])
        status, jobs = wsgi_request(
            self.app, "GET", "/documents/doc-empty-cite/relocation-jobs"
        )
        self.assertEqual(status, 200)
        self.assertEqual(jobs, [])

    def test_relocation_picks_up_new_page_and_rectangle(self):
        vid1 = upload(self.app, "doc-geo", [{
            "block_id": "g1", "page": 1, "text": "地理坐標引用",
            "rect": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0},
        }])
        status, cite = post_json(
            self.app,
            "/documents/doc-geo/citations",
            {"version_id": vid1, "block_id": "g1", "char_start": 4, "char_end": 6},
        )
        cid = cite["citation_id"]
        # Same block text but moved to another page with a new rectangle.
        vid2 = upload(self.app, "doc-geo", [{
            "block_id": "g1", "page": 7, "text": "地理坐標引用",
            "rect": {"x0": 60.0, "y0": 70.0, "x1": 90.0, "y1": 80.0},
        }])
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "located")
        self.assertEqual(body["page"], 7)
        self.assertEqual(body["rect"],
                         {"x0": 60.0, "y0": 70.0, "x1": 90.0, "y1": 80.0})

    def test_quote_with_internal_whitespace_relocates_by_verbatim_text(self):
        vid1 = upload(self.app, "doc-ws3", [block("w1", 1, "ab  cd  ef")])
        status, cite = post_json(
            self.app,
            "/documents/doc-ws3/citations",
            {"version_id": vid1, "block_id": "w1", "char_start": 0, "char_end": 6},
        )
        self.assertEqual(cite["quote"], "ab  cd")
        cid = cite["citation_id"]
        # Whitespace reflow inside the block; verbatim quote now occurs once.
        vid2 = upload(self.app, "doc-ws3", [block("w1", 1, "xx ab  cd tail")])
        drain_jobs()
        status, body = wsgi_request(self.app, "GET", f"/citations/{cid}")
        self.assertEqual(body["state"], "located")
        self.assertEqual((body["char_start"], body["char_end"]), (3, 9))
        conn = get_conn()
        try:
            row = conn.execute(
                f"SELECT text FROM blocks_{vid2} WHERE block_id = 'w1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["text"][3:9], "ab  cd")

    def test_interleaved_batches_and_confirmation_are_consistent(self):
        # Many citations; process the relocation one small batch at a time and
        # confirm a pending row in between, proving the conditional writes
        # never cross-contaminate states.
        n = 50
        records = [block(f"k{i:02d}", 1, f"unique-{i:02d} 錨點x") for i in range(n)]
        vid1 = upload(self.app, "doc-interleave", records)
        cids = []
        for i in range(n):
            status, cite = post_json(
                self.app,
                "/documents/doc-interleave/citations",
                {"version_id": vid1, "block_id": f"k{i:02d}",
                 "char_start": 10, "char_end": 12},
            )
            self.assertEqual(status, 201)
            cids.append(cite["citation_id"])
        # Half the blocks lose their quote; half keep it verbatim.
        new_records = []
        for i in range(n):
            if i % 2 == 0:
                new_records.append(block(f"k{i:02d}", 1, f"changed-{i} no anchor"))
            else:
                new_records.append(block(f"k{i:02d}", 1, f"unique-{i} 錨點"))
        upload(self.app, "doc-interleave", new_records)

        conn = get_conn()
        try:
            job_id = citations.claim_next_job(conn)
            # Drain with batch size 7 to force many interleaved transactions.
            done = False
            while not done:
                done = citations.process_job_batch(conn, job_id, 7)
        finally:
            conn.close()

        status, review = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-interleave/citations",
            query={"status": "pending_review", "limit": 200},
        )
        review_ids = {i["citation_id"] for i in review["items"]}
        self.assertEqual(review_ids, {cids[i] for i in range(0, n, 2)})

        # Confirm every pending row onto its current-version block. The
        # pending rows correspond exactly to the even-indexed blocks.
        for idx in range(0, n, 2):
            cid = cids[idx]
            status, body = post_json(
                self.app,
                f"/citations/{cid}/confirm",
                {"block_id": f"k{idx:02d}", "char_start": 0, "char_end": 7},
            )
            self.assertEqual(status, 200, body)

        status, page = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-interleave/citations",
            query={"limit": 200},
        )
        self.assertEqual(page["total"], n)
        self.assertTrue(all(i["state"] == "located" for i in page["items"]))
        self.assertTrue(all(i["rect"] is not None for i in page["items"]))


if __name__ == "__main__":
    unittest.main()
