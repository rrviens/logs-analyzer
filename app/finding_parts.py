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
# What a line means, for its marker: "do" = something you could have done, "ok" = fine / not a mistake, "" = context.
DO = re.compile(r"would likely have saved them|ready and unused|but was pressed at .+ later|and not pressed before"
                r"|^Yes - (ready since|not pressed yet)|sat ready through|^Unused while ready")
OK = re.compile(r"not a mistake|holding was reasonable|pressed as soon as it was available|handled without yours"
                r"|^No - on cooldown")
LABEL_MARK = {"What to do": "do", "No reaction window": "ok"}


def mark(text: str, label: str = "") -> str:
    return LABEL_MARK.get(label) or ("do" if DO.search(text) else "ok" if OK.search(text) else "")


def structure(detail: list[str], context: list[list[str]]) -> dict:
    """{verdict: [[label, text]], shares: {seconds, rows: [[name, pct]]} | None, rates: [[name, mine, peer, note]],
    why: [{text, sub, mark}], peers: [...], notes: [...], verdict_lines: [{label, lines: [{text, mark}]}]} - indented lines nest under the line before them."""
    out = {"verdict": [c for c in context if c[0] not in PEER_CONTEXT],
           # the same, line by line with a marker each (the page shows these; "verdict" stays for older pages)
           "verdict_lines": [{"label": k, "lines": [{"text": t, "mark": mark(t, k)} for t in v.split("\n")]}
                             for k, v in context if k not in PEER_CONTEXT],
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
            if mark(line) == "do":
                last["mark"] = "do"  # a late or missed press below it makes the whole line one to act on
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
            last = {"text": line, "sub": [], "mark": ""}
            out["notes"].append(last)
            continue
        last = {"text": line, "sub": [], "mark": mark(line)}
        out["peers" if PEER.search(line) else "why"].append(last)
    return out
