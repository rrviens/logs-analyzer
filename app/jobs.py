"""Background jobs kept in the database, so they survive restarts and wait out Warcraft Logs rate limits.

JOB_WORKERS threads run them (1 on a small host keeps memory flat). A job someone clicked runs before keys queued in
the background, and one person's jobs run one at a time (they share that person's rate limit). A job that hits a rate limit is not
an error: it goes back to "waiting" with a retry time (WCL's Retry-After, else a growing backoff) and the page shows
that instead. Progress lines live in memory only; status, notes and errors are in the table.
"""
import json
import logging
import os
import threading
import time
import traceback
import uuid
from typing import Callable

from app.wcl import WCLClient, WCLError

log = logging.getLogger("uvicorn.error")
BACKOFF_S = [60, 120, 300, 600, 900, 1800]  # waits between attempts on a rate limit (when WCL gives no Retry-After)
MAX_ATTEMPTS = len(BACKOFF_S) + 1
KEEP_DAYS = 7  # finished jobs older than this are deleted
JOB_WORKERS = max(1, int(os.getenv("JOB_WORKERS", "1")))
# Clicked jobs first (background "auto" ones last), then by due time. Used for picking and for queue positions.
ORDER = "COALESCE(json_extract(payload, '$.auto'), 0), next_at, rowid"


class Jobs:
    def __init__(self, db_client: WCLClient):
        self.c = db_client
        self.handlers: dict[str, Callable] = {}
        self.progress: dict[str, list[str]] = {}
        self.previews: dict[str, dict] = {}  # partial results to show while a job runs (memory only)
        self.wake = threading.Event()
        with self.c.lock:
            self.c.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner INTEGER, kind TEXT, payload TEXT, "
                              "status TEXT, attempts INTEGER, next_at REAL, note TEXT, error TEXT, result_key TEXT, "
                              "created REAL, updated REAL)")
            # A restart interrupted these: run them again.
            self.c.db.execute("UPDATE jobs SET status = 'queued', note = NULL WHERE status = 'running'")
            self.c.db.execute("DELETE FROM jobs WHERE status IN ('done', 'error') AND updated < ?",
                              (time.time() - KEEP_DAYS * 86400,))
            self.c.db.commit()

    def handler(self, kind: str):
        def register(fn):
            self.handlers[kind] = fn
            return fn
        return register

    def start(self) -> None:
        for _ in range(JOB_WORKERS):
            threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, owner: int, kind: str, payload: dict) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.c.lock:
            self.c.db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, 'queued', 0, ?, NULL, NULL, NULL, ?, ?)",
                              (job_id, owner, kind, json.dumps(payload), now, now, now))
            self.c.db.commit()
        self.progress[job_id] = []
        self.wake.set()
        return job_id

    def pending(self, owner: int, kind: str, url: str) -> str | None:
        """An unfinished job of this owner for this URL (so a click or a refresh doesn't queue it twice)."""
        with self.c.lock:
            rows = self.c.db.execute("SELECT id, payload FROM jobs WHERE owner = ? AND kind = ? AND status IN "
                                     "('queued', 'running', 'waiting') ORDER BY rowid DESC", (owner, kind)).fetchall()
        return next((jid for jid, p in rows if json.loads(p).get("url") == url), None)

    def prioritize(self, job_id: str) -> None:
        """Someone clicked a key that was queued in the background: it now goes ahead of background jobs."""
        with self.c.lock:
            self.c.db.execute("UPDATE jobs SET payload = json_remove(payload, '$.auto') WHERE id = ?", (job_id,))
            self.c.db.commit()
        self.wake.set()

    def busy(self) -> bool:
        """Someone's analysis is queued or running (background chores wait for it)."""
        with self.c.lock:
            return self.c.db.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone() is not None

    def get(self, job_id: str) -> dict | None:
        with self.c.lock:
            row = self.c.db.execute("SELECT id, owner, kind, payload, status, attempts, next_at, note, error, result_key "
                                    "FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        keys = ["id", "owner", "kind", "payload", "status", "attempts", "next_at", "note", "error", "result_key"]
        job = dict(zip(keys, row))
        job["payload"] = json.loads(job["payload"])
        job["progress"] = self.progress.get(job_id, [])
        job["preview"] = self.previews.get(job_id) if job["status"] in ("running", "waiting", "queued") else None
        job["position"] = self._position(job) if job["status"] == "queued" else None
        return job

    def _position(self, job: dict) -> int:
        with self.c.lock:
            ids = [r[0] for r in self.c.db.execute(f"SELECT id FROM jobs WHERE status = 'queued' ORDER BY {ORDER}")]
        return ids.index(job["id"]) if job["id"] in ids else 0

    def _set(self, job_id: str, **fields) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.c.lock:
            self.c.db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
            self.c.db.commit()

    def _next(self) -> tuple | None:
        """Pick and claim the next job in one step under the lock, so two workers never take the same one. Skips
        owners who already have a job running."""
        with self.c.lock:
            job = self.c.db.execute("SELECT id, owner, kind, payload, attempts FROM jobs WHERE status IN ('queued', 'waiting') "
                                    "AND next_at <= ? AND owner NOT IN (SELECT owner FROM jobs WHERE status = 'running') "
                                    f"ORDER BY {ORDER} LIMIT 1", (time.time(),)).fetchone()
            if job:
                self.c.db.execute("UPDATE jobs SET status = 'running', note = NULL, updated = ? WHERE id = ?", (time.time(), job[0]))
                self.c.db.commit()
            return job

    def _soonest_wait(self) -> float:
        with self.c.lock:
            row = self.c.db.execute("SELECT MIN(next_at) FROM jobs WHERE status IN ('queued', 'waiting')").fetchone()
        return max(1.0, min(60.0, (row[0] or time.time() + 60) - time.time()))

    def _loop(self) -> None:
        while True:
            job = self._next()
            if not job:
                # shortcut: while a due job waits for its owner's running one, idle workers re-check every second
                self.wake.wait(self._soonest_wait())
                self.wake.clear()
                continue
            job_id, owner, kind, payload, attempts = job
            progress = self.progress.setdefault(job_id, [])
            try:
                key = self.handlers[kind](owner, json.loads(payload), progress.append,
                                          lambda part, j=job_id: self.previews.__setitem__(j, part))
                self.previews.pop(job_id, None)
                self._set(job_id, status="done", result_key=key, note=None)
            except WCLError as e:
                if e.rate_limited and attempts + 1 < MAX_ATTEMPTS:
                    wait = e.retry_after or BACKOFF_S[min(attempts, len(BACKOFF_S) - 1)]
                    wait = min(wait, 3600)
                    at = time.strftime("%H:%M", time.gmtime(time.time() + wait))
                    log.warning("job %s (%s, owner %s) rate limited, attempt %d - retrying in %ds", job_id[:8], kind,
                                owner, attempts + 1, wait)
                    self._set(job_id, status="waiting", attempts=attempts + 1, next_at=time.time() + wait,
                              note=f"Warcraft Logs is still processing the end of this key - retrying in {_dur(wait)} "
                                   f"(at {at} UTC)." if e.uploading else
                                   f"Waiting for Warcraft Logs (it's limiting requests) - retrying in "
                                   f"{_dur(wait)} (at {at} UTC). Everything fetched so far is saved.")
                else:
                    log.warning("job %s (%s, owner %s) failed: %s", job_id[:8], kind, owner, e)
                    self._set(job_id, status="error", error=str(e))
                    self.previews.pop(job_id, None)
            except Exception as e:  # noqa: BLE001 - report to the page, full traceback to the log
                log.error("job %s (%s, owner %s) crashed:\n%s", job_id[:8], kind, owner, traceback.format_exc())
                self._set(job_id, status="error", error=_friendly(e))
                self.previews.pop(job_id, None)


def _dur(s: float) -> str:
    return f"{round(s)}s" if s < 90 else f"{round(s / 60)} min"


def _friendly(e: Exception) -> str:
    from app.pipeline import AnalysisError  # local: avoid an import cycle
    if isinstance(e, (AnalysisError, ValueError, StopIteration)):
        return str(e) or "Could not read that log."
    return f"Something went wrong while analyzing ({type(e).__name__}). Try again."
