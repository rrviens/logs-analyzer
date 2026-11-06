"""A finding's detail lines, sorted for reading: what was ready (context), the evidence (damage shares as numbers,
yours-vs-peers rates, what happened), how peers compare, and side notes. The rules write plain sentences; this is
the one place that knows their shapes, and tests/test_finding_parts.py pins them so a reworded rule fails loudly
instead of landing in the wrong group. The plain `detail` list stays as it was (the classic page reads it)."""
import re

SHARE = re.compile(r"^(?P<name>.+?): (?P<pct>\d+)% of the damage in the last (?P<s>\d+)s$")
RATE = re.compile(r"^(?P<name>.+?): (?P<mine>[\d.]+)% group HP/s vs (?P<peer>[\d.]+)%(?: - (?P<note>.+))?$")
PEER = re.compile(r"\bpeers?\b|\btop players\b|\btop-run\b|^top \w|\beven top\b", re.I)
NOTE = re.compile(r"^Also\b")
PEER_CONTEXT = ("Top players", "Peers")


def structure(detail: list[str], context: list[list[str]]) -> dict:
    """{verdict: [[label, text]], shares: {seconds, rows: [[name, pct]]} | None, rates: [[name, mine, peer, note]],
    why: [{text, sub}], peers: [{text, sub}], notes: [{text, sub}]} - indented lines nest under the line before them."""
    out = {"verdict": [c for c in context if c[0] not in PEER_CONTEXT],
           "peer_context": [c for c in context if c[0] in PEER_CONTEXT],
           "shares": None, "rates": [], "rates_title": "", "why": [], "peers": [], "notes": []}
    last = None
    for raw in detail:
        line = raw.strip()
        if raw.startswith(" ") and (m := RATE.match(line)):
            out["rates"].append([m["name"], float(m["mine"]), float(m["peer"]), m["note"] or ""])
            continue
        if raw.startswith(" ") and last is not None:
            last["sub"].append(line)
            continue
        if line.endswith("Biggest differences:"):  # the intro to the rate lines below: their table's title
            out["rates_title"] = line.removesuffix(" Biggest differences:")
            last = None
            continue
        if m := SHARE.match(line):
            out["shares"] = out["shares"] or {"seconds": int(m["s"]), "rows": []}
            out["shares"]["rows"].append([m["name"], int(m["pct"])])
            last = None
            continue
        if NOTE.match(line):
            last = {"text": line, "sub": []}
            out["notes"].append(last)
            continue
        last = {"text": line, "sub": []}
        out["peers" if PEER.search(line) else "why"].append(last)
    return out
