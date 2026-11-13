#!/usr/bin/env python3
"""Offline HTTP smoke test.

Starts the (shim-backed) WSGI application on a daemon thread and drives the
whole flow over real HTTP using only the standard library: seeding, cross-page
search, correction/replacement and validation errors. The production stack is
the same application object served by uvicorn; this script exists solely for
the package-index-free sandbox.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if importlib.util.find_spec("fastapi") is None:
    sys.path.insert(0, str(ROOT / "tests" / "_shim"))

WORKDIR = Path(tempfile.mkdtemp(prefix="archive-smoke-"))
os.environ["ARCHIVE_DATA_DIR"] = str(WORKDIR)
os.environ["ARCHIVE_DB_PATH"] = str(WORKDIR / "smoke.sqlite3")

from app.main import app  # noqa: E402


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass


def http(method: str, url: str, data: bytes | None = None,
         content_type: str = "application/x-ndjson"):
    headers = {}
    if data is not None:
        headers["content-type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def http_json(method: str, url: str, payload: dict):
    return http(method, url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                content_type="application/json")


def wait_until(deadline_seconds: float, predicate, label: str):
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {label}")


def main() -> int:
    httpd = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    failures = []

    def check(label: str, condition: bool, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {label} {detail}")
        if not condition:
            failures.append(label)

    try:
        # 1. health
        status, body = http("GET", f"{base}/health")
        check("health endpoint", status == 200 and body["status"] == "ok")

        # 2. upload the two demo documents
        for name in ("gazette", "ledger"):
            payload = (ROOT / "demo" / f"{name}.ndjson").read_bytes()
            status, body = http("PUT", f"{base}/documents/{name}", payload)
            check(f"upload {name}", status == 202, str(body))

        # 3. cross-page hit at the page 1 -> page 2 boundary (the full stop
        #    is part of the original text, so it must appear in the query)
        q = urllib.parse.quote("可用。第二條")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        ok = (
            status == 200
            and body["result_count"] == 1
            and body["hits"][0]["crosses_pages"] is True
            and [s["page"] for s in body["hits"][0]["spans"]] == [1, 2]
        )
        check("cross-page match at page boundary", ok, json.dumps(body, ensure_ascii=False))

        # 3b. cross-block hit within a single page
        q = urllib.parse.quote("轉型，保障")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        ok = (
            status == 200
            and body["result_count"] == 1
            and body["hits"][0]["crosses_blocks"] is True
            and body["hits"][0]["crosses_pages"] is False
        )
        check("cross-block match on one page", ok, json.dumps(body, ensure_ascii=False))

        # 4. whitespace-insensitive query
        q = urllib.parse.quote("檢索 系統應當 忽略排版空白")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        check("whitespace-insensitive search", status == 200 and body["result_count"] == 1)

        # 5. overlapping hits are kept and ordered
        q = urllib.parse.quote("檔")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        starts = [h["compact_start"] for h in body["hits"]]
        check("overlapping/ordered hits", starts == sorted(starts) and len(starts) >= 2, str(starts))

        # 6. correction: full re-upload atomically switches versions
        corrected = (ROOT / "demo" / "gazette.ndjson").read_text().replace(
            "本規範自公 布之日起施行。",
            "本規範自公布之日起施行，並廢止舊版條款。",
        ).encode()
        status, body = http("PUT", f"{base}/documents/gazette", corrected)
        check("re-upload accepted", status == 202, str(body))
        new_vid = body["version_id"]
        status, body = http(
            "GET",
            f"{base}/documents/gazette/search?q={urllib.parse.quote('廢止舊版條款')}",
        )
        check(
            "new version live after correction",
            status == 200 and body["result_count"] == 1 and body["version_id"] == new_vid,
        )

        # 7. bad line fails the replacement and leaves old version usable
        bad = (ROOT / "demo" / "gazette.ndjson").read_text().encode() + b"{broken json\n"
        status, body = http("PUT", f"{base}/documents/gazette", bad)
        check("malformed line rejected with 400", status == 400 and "line" in body["detail"], str(body))
        status, body = http(
            "GET",
            f"{base}/documents/gazette/search?q={urllib.parse.quote('廢止舊版條款')}",
        )
        check("old version still searchable after failed replacement",
              status == 200 and body["version_id"] == new_vid)

        # 8. query validation
        status, _ = http("GET", f"{base}/documents/gazette/search?q=" + urllib.parse.quote(" "))
        check("blank query rejected with 400", status == 400)
        status, _ = http(
            "GET", f"{base}/documents/gazette/search?q=" + urllib.parse.quote("a" * 81)
        )
        check("81-char query rejected with 400", status == 400)

        # 9. unknown document
        status, _ = http("GET", f"{base}/documents/nope/search?q=x")
        check("unknown document returns 404", status == 404)

        # 10. citation capture, cross-version relocation and review ----------
        # Upload a dedicated document so the flow is self-contained.
        cite_blocks = [
            {"block_id": "c1", "page": 1,
             "text": "第一條 本市檔案館應當保障電子文檔真實完整。",
             "rect": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}},
            {"block_id": "c2", "page": 2,
             "text": "第二條 引用定位需要跨換版保持可用。",
             "rect": {"x0": 5.0, "y0": 6.0, "x1": 7.0, "y1": 8.0}},
        ]
        status, body = http(
            "PUT", f"{base}/documents/cite-doc",
            ("\n".join(json.dumps(r, ensure_ascii=False) for r in cite_blocks) + "\n").encode(),
        )
        check("citation document uploaded", status == 202, str(body))
        v1 = body["version_id"]

        # 10a. capture a citation on the current version
        #      c2 text: "第二條 引用定位需要跨換版保持可用。" -> 引用定位 at 4:8
        status, cite = http_json(
            "POST", f"{base}/documents/cite-doc/citations",
            {"version_id": v1, "block_id": "c2", "char_start": 4, "char_end": 8},
        )
        check("citation created", status == 201 and cite["state"] == "located", str(cite))
        check("citation coordinates served", cite["block_id"] == "c2"
              and cite["rect"]["x1"] == 7.0 and cite["quote"] == "引用定位")
        cid = cite["citation_id"]

        # 10b. stale version is rejected with 409
        status, body = http_json(
            "POST", f"{base}/documents/cite-doc/citations",
            {"version_id": v1 + 999, "block_id": "c2", "char_start": 0, "char_end": 3},
        )
        check("stale version citation rejected", status == 409, str(body))

        # 10c. new version: c2 keeps identical text -> range carried over;
        #      c1 text changes and the quote vanishes -> pending review.
        new_blocks = [
            {"block_id": "c1", "page": 1,
             "text": "第一條 修訂後的條款文本完全不同。",
             "rect": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}},
            {"block_id": "c2", "page": 2,
             "text": "第二條 引用定位需要跨換版保持可用。",
             "rect": {"x0": 5.0, "y0": 6.0, "x1": 7.0, "y1": 8.0}},
        ]
        status, body = http(
            "PUT", f"{base}/documents/cite-doc",
            ("\n".join(json.dumps(r, ensure_ascii=False) for r in new_blocks) + "\n").encode(),
        )
        check("cite-doc re-upload accepted", status == 202)
        v2 = body["version_id"]

        # While relocation runs, coordinates must be hidden (no stale data).
        status, page = http("GET", f"{base}/documents/cite-doc/citations?limit=10")
        check("no stale coordinates while relocating",
              all(i["rect"] is None and i["block_id"] is None
                  for i in page["items"]), json.dumps(page, ensure_ascii=False))

        # The real background worker thread drains the durable job queue.
        wait_until(
            15.0,
            lambda: all(
                i["state"] in ("located", "pending_review")
                for i in http("GET", f"{base}/documents/cite-doc/citations?limit=10")[1]["items"]
            ),
            "relocation worker finishes",
        )
        status, page = http("GET", f"{base}/documents/cite-doc/citations?limit=10")
        by_id = {i["citation_id"]: i for i in page["items"]}
        relocated = by_id[cid]
        check("identical block keeps range after switch",
              relocated["state"] == "located"
              and relocated["version_id"] == v2
              and (relocated["char_start"], relocated["char_end"]) == (4, 8),
              json.dumps(relocated, ensure_ascii=False))

        # 10d. create a citation whose quote becomes ambiguous in v3.
        # v2 c1 text: "第一條 修訂後的條款文本完全不同。" -> 條款 at 8:10
        status, amb = http_json(
            "POST", f"{base}/documents/cite-doc/citations",
            {"version_id": v2, "block_id": "c1", "char_start": 8, "char_end": 10},
        )
        check("second citation created", status == 201, str(amb))
        amb_id = amb["citation_id"]
        v3_blocks = [
            {"block_id": "c1", "page": 1,
             "text": "條款 條款 重複出現的條款文本。",
             "rect": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}},
            {"block_id": "c2", "page": 2,
             "text": "第二條 引用定位需要跨換版保持可用。",
             "rect": {"x0": 5.0, "y0": 6.0, "x1": 7.0, "y1": 8.0}},
        ]
        status, body = http(
            "PUT", f"{base}/documents/cite-doc",
            ("\n".join(json.dumps(r, ensure_ascii=False) for r in v3_blocks) + "\n").encode(),
        )
        check("cite-doc third version accepted", status == 202)
        wait_until(
            15.0,
            lambda: http("GET", f"{base}/citations/{amb_id}")[1].get("state")
                    in ("located", "pending_review"),
            "ambiguous citation reaches review",
        )
        status, body = http("GET", f"{base}/citations/{amb_id}")
        check("ambiguous quote flagged for review",
              status == 200 and body["state"] == "pending_review"
              and body["rect"] is None and "quote_ambiguous" in (body["review_reason"] or ""),
              json.dumps(body, ensure_ascii=False))

        # Manual confirmation anchors it on the current version.
        # v3 c1 text "條款 條款 重複出現的條款文本。" -> 重複 at 6:8.
        status, confirmed = http_json(
            "POST", f"{base}/citations/{amb_id}/confirm",
            {"block_id": "c1", "char_start": 6, "char_end": 8},
        )
        check("manual confirmation locates citation",
              status == 200 and confirmed["state"] == "located"
              and confirmed["quote"] == "重複",
              json.dumps(confirmed, ensure_ascii=False))
        # Confirming twice conflicts.
        status, body = http_json(
            "POST", f"{base}/citations/{amb_id}/confirm",
            {"block_id": "c1", "char_start": 0, "char_end": 2},
        )
        check("second confirmation conflicts", status == 409)

        # Job queue visibility and final pagination.
        status, jobs = http("GET", f"{base}/documents/cite-doc/relocation-jobs")
        check("relocation jobs recorded and finished/superseded",
              status == 200 and len(jobs) >= 2
              and all(j["state"] in ("done", "superseded") for j in jobs),
              json.dumps(jobs, ensure_ascii=False))
        status, review_page = http(
            "GET", f"{base}/documents/cite-doc/citations?status=pending_review&limit=5"
        )
        check("review queue filter works",
              status == 200 and all(i["state"] == "pending_review"
                                    for i in review_page["items"]))
    finally:
        httpd.shutdown()
        shutil.rmtree(WORKDIR, ignore_errors=True)

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
