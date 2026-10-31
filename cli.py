"""Usage:
    python cli.py check
    python cli.py analyze <report url> [--player NAME] [--top 8] [--all] [--fresh]

For everyday use, start the web app instead (start.bat).
"""
import argparse

from app.config import CACHE_PATH, wcl_credentials
from app.pipeline import AnalysisError, run_analysis
from app.wcl import WCLClient

DEFAULT_SHOWN = 12


def cmd_check(client: WCLClient):
    data = client.query("{ rateLimitData { limitPerHour pointsSpentThisHour } }", ttl=0)
    r = data["rateLimitData"]
    print(f"WCL API OK - {r['pointsSpentThisHour']:.0f}/{r['limitPerHour']} points used this hour")


def cmd_analyze(client: WCLClient, url: str, player_name: str | None, top: int, show_all: bool, fresh: bool):
    try:
        res = run_analysis(client, url, player_name, top, progress=print, use_cache=not fresh)
    except AnalysisError as e:
        raise SystemExit(str(e))

    r, comp = res["run"], res["compared"]
    print(f"\n{r['dungeon']} +{r['level']} - {r['player']} - {r['duration_min']} min")
    print(f"Compared against {comp['count']} top Resto Shaman logs (keys {', '.join(f'+{l}' for l in comp['levels'])})")
    print(f"Context model: {'top runs ' + comp['model'] if comp['model'] else 'none - run study.py'}")

    findings = res["findings"]
    shown = findings if show_all else findings[:DEFAULT_SHOWN]
    print(f"\n=== Findings ({len(shown)} of {len(findings)}, most impactful first) ===")
    for f in shown:
        print(f"\n[{f['severity']:>3}] {f['clock']}  {f['title']}")
        print(f"      {f['pull']}")
        for line in f["detail"]:
            print(f"      - {line}")

    if res["good_calls"]:
        print("\n=== Good calls ===")
        for f in res["good_calls"]:
            print(f"  {f['title']} ({f['pull']}) - {' '.join(f['detail'])}")

    print("\n=== Per-pull requirements (vs top groups) ===")
    print(f"{'Pull':<38}{'Time':>6}{'Group HP':>10}{'Top HP':>8}{'Dmg in/s':>10}{'Top in/s':>10}{'Heal/s':>9}{'Top heal':>10}")
    fmt_k = lambda v: f"{v / 1000:.0f}k" if v is not None else "-"
    fmt_p = lambda v: f"{v:.0%}" if v is not None else "-"
    for q in res["requirements"]:
        print(f"{q['pull'][:37]:<38}{q['duration_s']:>5.0f}s{fmt_p(q['group_hp']):>10}{fmt_p(q['top_group_hp']):>8}"
              f"{fmt_k(q['intake']):>10}{fmt_k(q['top_intake']):>10}{fmt_k(q['healing']):>9}{fmt_k(q['top_healing']):>10}")
    unmatched = sum(1 for q in res["requirements"] if not q["matched"])
    if unmatched:
        print(f"\n{unmatched} pull(s) had no matching pull in top logs; they use fallback thresholds.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    a = sub.add_parser("analyze")
    a.add_argument("url")
    a.add_argument("--player")
    a.add_argument("--top", type=int, default=8)
    a.add_argument("--all", action="store_true", help="show every finding, not just the top ones")
    a.add_argument("--fresh", action="store_true", help="ignore the saved result and re-analyze")
    args = parser.parse_args()

    client = WCLClient(*wcl_credentials(), CACHE_PATH)
    if args.cmd == "check":
        cmd_check(client)
    else:
        cmd_analyze(client, args.url, args.player, args.top, args.all, args.fresh)


if __name__ == "__main__":
    main()
