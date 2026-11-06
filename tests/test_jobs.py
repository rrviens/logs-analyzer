"""Job queue: clicked jobs go before background ones, one job per person at a time, and parallel workers never run
a job twice. Run: .venv/Scripts/python.exe -m tests.test_jobs"""
import tempfile
import threading
import time
from pathlib import Path

from app import jobs
from app.wcl import WCLClient


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        q = jobs.Jobs(WCLClient("id", "secret", Path(d) / "c.sqlite3"))
        auto = q.submit(1, "analyze", {"url": "a", "auto": True})
        click = q.submit(2, "analyze", {"url": "b"})
        same_owner = q.submit(2, "analyze", {"url": "c"})
        assert q.get(click)["position"] == 0 and q.get(auto)["position"] == 2
        assert q._next()[0] == click  # clicked first, though queued later
        assert q._next()[0] == auto   # owner 2 is busy, so their second job waits
        assert q._next() is None
        q.prioritize(same_owner)  # harmless on a job that was never "auto"

        # Two workers, many jobs from many owners: each runs exactly once, never two of one owner at once.
        q2 = jobs.Jobs(WCLClient("id", "secret", Path(d) / "c2.sqlite3"))
        runs, lock, active = [], threading.Lock(), set()

        @q2.handler("analyze")
        def work(owner, payload, progress, preview):
            with lock:
                assert owner not in active, "two jobs of one owner at once"
                active.add(owner)
                runs.append(payload["url"])
            time.sleep(0.02)
            with lock:
                active.discard(owner)
            return "k"

        ids = [q2.submit(i % 3, "analyze", {"url": str(i)}) for i in range(12)]
        jobs.JOB_WORKERS = 2
        q2.start()
        deadline = time.time() + 30
        while time.time() < deadline and any(q2.get(j)["status"] != "done" for j in ids):
            time.sleep(0.05)
        assert all(q2.get(j)["status"] == "done" for j in ids), [q2.get(j)["error"] for j in ids]
        assert sorted(runs, key=int) == [str(i) for i in range(12)], runs
    print("job checks ok")


if __name__ == "__main__":
    main()
