#!/usr/bin/env python3
"""End-to-end demo against a running API (stdlib only).

Walks through: cross-page search, whitespace-insensitive matching, overlapping
hits, and a correction via full-document re-upload.

    ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/demo_requests.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("ARCHIVE_API_BASE", "http://localhost:8000").rstrip("/")
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def call(method: str, path: str, data: bytes | None = None,
         content_type: str = "application/x-ndjson") -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": content_type} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def call_json(method: str, path: str, payload: dict) -> tuple[int, dict]:
    return call(method, path, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                content_type="application/json")


def wait_for_state(citation_id: int, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call("GET", f"/citations/{citation_id}")
        if status == 200 and body["state"] in ("located", "pending_review"):
            return body
        time.sleep(0.2)
    raise SystemExit(f"citation {citation_id} never finished relocating")


def heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_search(query: str) -> None:
    status, body = call("GET", f"/documents/gazette/search?q={urllib.parse.quote(query)}")
    print(f"\nGET /documents/gazette/search?q={query!r}  -> HTTP {status}")
    if status != 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    print(f"result_count={body['result_count']} has_more={body['has_more']} "
          f"version={body['version_id']}")
    for i, hit in enumerate(body["hits"][:5], 1):
        pages = sorted({s["page"] for s in hit["spans"]})
        blocks = [s["block_id"] for s in hit["spans"]]
        ranges = [f"{s['char_start']}:{s['char_end']}" for s in hit["spans"]]
        print(f"  #{i} compact[{hit['compact_start']}:{hit['compact_end']}] "
              f"pages={pages} blocks={blocks} char_ranges={ranges}")
        print(f"      matched_text={hit['matched_text']!r} "
              f"crosses_pages={hit['crosses_pages']}")


def main() -> int:
    heading("0. Health and documents")
    print(call("GET", "/health"))
    print(call("GET", "/documents"))

    heading("1. Cross-page match (phrase split across scan pages 1 and 2)")
    # Page 1 ends "...真實、完整與可用。" and page 2 begins "第二條 ...", so
    # this phrase straddles the two scan pages (punctuation is part of text).
    show_search("可用。第二條")
    # A cross-block hit that stays on the same page.
    show_search("轉型，保障")

    heading("2. Whitespace-insensitive query (arbitrary spaces in the query)")
    show_search("檢索 系統應當 忽略排版空白")

    heading("3. Single-character query (unigram index, ordered hits)")
    show_search("檔")

    heading("4. No-match response")
    show_search("不存在的詞組xyz")

    heading("5. Correction: full re-upload replaces the document atomically")
    corrected = (DEMO_DIR / "gazette.ndjson").read_text().replace(
        "本規範自公 布之日起施行。",
        "本規範自公布之日起施行，並廢止舊版條款。",
    ).encode("utf-8")
    status, payload = call("PUT", "/documents/gazette", corrected)
    print(f"PUT /documents/gazette -> HTTP {status}: {payload}")
    time.sleep(1.0)
    print("versions after replacement:")
    print(json.dumps(call("GET", "/documents/gazette/versions")[1],
                     ensure_ascii=False, indent=2))
    show_search("廢止舊版條款")

    heading("6. Citation capture, cross-version relocation and review")
    # Use the currently active version of gazette for the citation.
    status, docs = call("GET", "/documents")
    gazette_vid = next(d["active_version_id"] for d in docs if d["doc_id"] == "gazette")
    print(f"active gazette version: {gazette_vid}")

    # Capture an excerpt inside block gazette-p2-b2.
    block_text = "檢索系統 應當忽略排版空白並支持跨塊、跨頁的原文定位。"
    quote = "忽略排版空白"
    q_start = block_text.find(quote)
    status, cite = call_json(
        "POST", "/documents/gazette/citations",
        {"version_id": gazette_vid, "block_id": "gazette-p2-b2",
         "char_start": q_start, "char_end": q_start + len(quote)},
    )
    print(f"POST citations -> HTTP {status}")
    print(json.dumps(cite, ensure_ascii=False, indent=2))
    citation_id = cite["citation_id"]

    # A stale version id is rejected.
    status, stale = call_json(
        "POST", "/documents/gazette/citations",
        {"version_id": gazette_vid - 1, "block_id": "gazette-p2-b2",
         "char_start": 0, "char_end": 1},
    )
    print(f"stale-version citation -> HTTP {status}: {stale['detail']}")

    # Re-upload a corrected version: the cited block keeps identical text, so
    # the background relocation must carry the same character range over.
    corrected = (DEMO_DIR / "gazette.ndjson").read_text().replace(
        "本規範自公 布之日起施行。",
        "本規範自公布之日起施行，新增條款。",
    ).encode("utf-8")
    status, payload = call("PUT", "/documents/gazette", corrected)
    print(f"re-upload gazette -> HTTP {status}, new version {payload['version_id']}")
    moved = wait_for_state(citation_id)
    print("after background relocation:")
    print(json.dumps(moved, ensure_ascii=False, indent=2))

    # A second citation whose quote becomes ambiguous ends up pending review.
    # In this version "檢索" appears once at the start of the block.
    status, amb = call_json(
        "POST", "/documents/gazette/citations",
        {"version_id": payload["version_id"], "block_id": "gazette-p2-b2",
         "char_start": 0, "char_end": 2},
    )
    amb_id = amb["citation_id"]
    # The corrected block starts "檢索檢索系統 ...": the excerpt "檢索" now
    # occurs twice, so automatic relocation must refuse to guess.
    ambiguous = (DEMO_DIR / "gazette.ndjson").read_text().replace(
        "檢索系統 應當忽略排版空白並支持跨塊、跨頁的原文定位。",
        "檢索檢索系統 應當忽略排版空白並支持跨塊、跨頁的原文定位。",
    ).replace(
        "本規範自公 布之日起施行。",
        "本規範自公布之日起施行。",
    ).encode("utf-8")
    status, payload2 = call("PUT", "/documents/gazette", ambiguous)
    reviewed = wait_for_state(amb_id)
    print(f"ambiguous citation {amb_id} -> state={reviewed['state']} "
          f"reason={reviewed['review_reason']}")

    # Human confirms the intended occurrence on the current version.
    new_text = "檢索檢索系統 應當忽略排版空白並支持跨塊、跨頁的原文定位。"
    new_start = new_text.find("檢索系統")
    status, confirmed = call_json(
        "POST", f"/citations/{amb_id}/confirm",
        {"block_id": "gazette-p2-b2",
         "char_start": new_start, "char_end": new_start + 4},
    )
    print(f"manual confirmation -> HTTP {status}")
    print(json.dumps(confirmed, ensure_ascii=False, indent=2))

    # Paginated review queue and job audit trail.
    status, page = call(
        "GET",
        "/documents/gazette/citations?status=pending_review&limit=20&offset=0",
    )
    print(f"pending-review queue: total={page['total']} has_more={page['has_more']}")
    status, jobs = call("GET", "/documents/gazette/relocation-jobs")
    print("relocation jobs:")
    print(json.dumps(jobs, ensure_ascii=False, indent=2))

    heading("7. Recovery report (empty unless a restart recovered a crash)")
    print(call("GET", "/recovery"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
