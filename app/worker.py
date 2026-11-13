"""Background relocation worker.

A single daemon thread per process drains the durable ``relocation_jobs``
queue. It only ever holds one bounded batch of citations in memory, so its
footprint is independent of the total number of citations. Reads and text
scans happen outside write transactions (see
:func:`app.citations.process_job_batch`).

Version switches and manual confirmations are never blocked: the worker opens
its own short-lived connections and only takes brief write locks per batch.
Jobs are durable rows, so an interrupted worker resumes after restart instead
of losing work.
"""
from __future__ import annotations

import logging
import threading

from . import citations, config, db

logger = logging.getLogger("archive.relocate")


class RelocationWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._batch_size = max(1, config.RELOCATE_BATCH_SIZE)

    def start(self) -> None:
        if not config.RELOCATE_THREAD_ENABLED:
            logger.info("relocation worker thread disabled by configuration")
            return
        current = self._thread
        if current is not None and current.is_alive():
            return
        # Restart-safe: several app instances can share this process-wide
        # singleton (tests and embedded harnesses build more than one app),
        # and a previous lifespan may already have stopped an earlier thread.
        # A fresh event and thread object make every start independent.
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="relocation-worker", daemon=True
        )
        self._thread.start()
        # The thread is daemonized, so it cannot block interpreter shutdown;
        # under uvicorn the FastAPI lifespan performs an explicit stop().

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        # Detach the finished thread so a later start() is unconditionally
        # allowed even when stop() was called outside an active lifespan pair.
        self._thread = None

    def _run(self) -> None:
        # A private connection: request threads hold their own, and a
        # background job must never share an in-flight transaction.
        conn = db.get_conn()
        try:
            while not self._stop.is_set():
                try:
                    job_id = citations.claim_next_job(conn)
                    if job_id is None:
                        self._stop.wait(config.RELOCATE_POLL_INTERVAL)
                        continue
                    done = False
                    while not done and not self._stop.is_set():
                        done = citations.process_job_batch(
                            conn, job_id, self._batch_size
                        )
                except Exception:  # keep the worker alive across transient errors
                    logger.exception("relocation worker iteration failed; retrying")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = db.get_conn()
                    self._stop.wait(config.RELOCATE_POLL_INTERVAL)
        finally:
            conn.close()


# Process-wide singleton, started from the FastAPI lifespan.
worker = RelocationWorker()
