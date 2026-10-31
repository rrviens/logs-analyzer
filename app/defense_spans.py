"""Defensives active on each party member over time, from real buff uptimes in the log
(matched by name against data/defensives.json; escapes are left out). Area effects like Anti-Magic Zone
only buff the players standing in them, so the lanes show who was actually covered."""
import hashlib

from app.deaths import fetch_group_casts
from app.reports import Fight, Player
from app.runs import RunData, _events
from app.wcl import WCLClient

CACHE_VERSION = 3
PROTECTIVE = ("dr", "immune", "absorb", "max_hp")  # effects that count as "covered" for a hit


def fetch_defense_spans(client: WCLClient, fight: Fight, run: RunData, players: list[Player],
                        defensives: dict) -> list[dict]:
    """[{target, name, source, start, end, scope, protective}] - one per buff application."""
    tracked = {n: d for n, d in defensives.items() if not d["effect"].get("escape")}
    names_key = hashlib.sha1(",".join(sorted(tracked)).encode()).hexdigest()[:8]
    key = f"defspans:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{names_key}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    ids = {p.id for p in players}
    open_at: dict[tuple[int, str], tuple[int, int]] = {}
    spans = []
    for e in _events(client, fight, "Buffs", hostility="Friendlies"):
        tgt = e.get("targetID")
        if tgt not in ids:
            continue
        name = run.ability_name(e.get("abilityGameID", 0))
        if name not in tracked or tracked[name].get("from_cast"):
            continue
        if e["type"] in ("applybuff", "refreshbuff", "applybuffstack"):
            open_at.setdefault((tgt, name), (e["timestamp"], e.get("sourceID")))
        elif e["type"] == "removebuff" and (tgt, name) in open_at:
            start, src = open_at.pop((tgt, name))
            spans.append(_span(tgt, name, src, start, e["timestamp"], tracked[name]))
    for (tgt, name), (start, src) in open_at.items():
        spans.append(_span(tgt, name, src, start, fight.end, tracked[name]))
    # No buff in the log (e.g. Darkness): the cast covers the cast target, or the whole party for group ones.
    for t, src, aid, tgt in fetch_group_casts(client, fight, ids):
        name = run.ability_name(aid)
        d = tracked.get(name)
        if not d or not d.get("from_cast"):
            continue
        targets = ids if d["scope"] == "group" else [tgt if tgt in ids else src]
        spans.extend(_span(x, name, src, t, t + d["duration"] * 1000, d) for x in targets)
    spans.sort(key=lambda s: s["start"])
    client.set_cached(key, spans)
    return spans


def _span(target, name, source, start, end, d) -> dict:
    return {"target": target, "name": name, "source": source, "start": start, "end": end, "scope": d["scope"],
            "protective": any(k in d["effect"] for k in PROTECTIVE)}


def active_at(spans: list[dict], t: int, protective_only: bool = True) -> list[dict]:
    return [s for s in spans if s["start"] <= t <= s["end"] and (s["protective"] or not protective_only)]
