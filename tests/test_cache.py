"""Cache housekeeping: pruning keeps saved analyses, and a shared-cache clear leaves users' own copies.
Run: .venv/Scripts/python.exe -m tests.test_cache"""
import tempfile
from pathlib import Path

from app.wcl import WCLClient, _is_raw


def main() -> None:
    assert _is_raw("run:v9:abc:3:12") and _is_raw("u42:light:v1:abc:3") and _is_raw("a" * 64) and _is_raw("u7:" + "0" * 64)
    assert not _is_raw("analysis:v74:abc:3:me:8") and not _is_raw("u42:analysis_latest:abc:3:me:8")
    assert not _is_raw("recent_saved:x") and not _is_raw("rio:eu:kazzak:me") and not _is_raw("something_new:1")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:  # Windows: the open db can't be deleted
        c = WCLClient("id", "secret", Path(d) / "c.sqlite3")
        big = "x" * 200_000
        for i in range(40):
            c.set_cached(f"run:v1:code{i}:1:5", big)
        c.set_cached("analysis:v74:code0:1:me:8", big)  # oldest of all? no: written last, but must survive regardless
        c._db.execute("UPDATE cache SET created = 0 WHERE key LIKE 'analysis:%'")  # make it the oldest row
        c._db.commit()
        used = c.used_bytes()
        assert c.prune(used * 10, 0) == 0  # under the cap: nothing happens
        n = c.prune(used // 2, used // 4)
        assert n > 0 and c.used_bytes() <= used // 4 + 300_000, (n, c.used_bytes())
        assert c.get_cached("analysis:v74:code0:1:me:8") == big  # analyses are never pruned

        user = c.for_user("tok", 42)
        user.set_cached("run:v1:abc:3:5", "mine")
        c.set_cached("run:v1:abc:3:5", "shared")
        c.public().forget_fight("abc", 3)
        assert c.get_cached("run:v1:abc:3:5") is None and user.get_cached("run:v1:abc:3:5") == "mine"
        user.forget_fight("abc", 3)
        assert user.get_cached("run:v1:abc:3:5") is None
    print("cache checks ok")


if __name__ == "__main__":
    main()
