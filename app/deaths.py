"""Death recaps: the last seconds of hits and heals on the victim, which defensives were
ready - the group's, the victim's own and the healer's - and, for each ready one, a replay
of those seconds with it applied to estimate whether the player would have lived.

Defensive values live in data/defensives.json (approximate, user-editable).
"""
import json
from dataclasses import dataclass

from app.auras import Auras, removed_by
from app.charges import SAME_USE_MS, free_at, infer_charges, learn_recharge_ms, uses, wait_ms
from app.config import PROJECT_ROOT
from app.reports import Fight, Player
from app.runs import RunData, _events
from app.spellfacts import FACTS
from app.wcl import WCLClient

DEFENSIVES_PATH = PROJECT_ROOT / "data" / "defensives.json"
CACHE_VERSION = 1
GROUP_CASTS_VERSION = 2
WINDOW_MS = 10_000
LAST_CASTS_MS = 8_000
WAVE_MS = 1_000  # debuff applications this close are the same cast hitting several players
PULSE_COVERAGE, PULSE_MAX_HIT, PULSE_EVERY_S = 0.7, 0.2, 3.0  # "constant pulse": all pull, small, frequent
CAUSE_MIN_SHARE = 0.25
SELF_DISPEL_LATE_S = 2.0  # sitting in your own dispellable debuff longer than this is worth calling out
LIVED_MIN = 0.10  # replay bottoming out above this = likely lived; between 0 and this = too close to call
MAX_RECAPS = 10
# Healer dispel spells (a dispel event from one of these = the healer's own dispel can remove that debuff). Movement
# tools and shapeshifts also log as dispels, so they don't count.
DISPEL_SPELLS = {"Purify Spirit", "Purify", "Detox", "Cleanse", "Nature's Cure", "Naturalize", "Remove Corruption",
                 "Mass Dispel", "Cauterizing Flame", "Expunge", "Cleanse Toxins", "Cleanse Spirit"}
# Group cleanse totems: dispel name in the log -> (cast name, base cooldown s).
CLEANSE_TOTEMS = {"Poison Cleansing": ("Poison Cleansing Totem", 120)}  # cd checked on Wowhead, Nov 2026
LOW_HP, LOW_FOR_S = 0.5, 2.0  # under this HP for this long before dying = time to react with a personal
ENVIRONMENTAL = {"Falling", "Drowning", "Lava", "Fatigue", "Slime"}  # no defensive saves these: no "Use X" advice

DEATH_EVENTS_QUERY = """
query($code: String!, $fightIDs: [Int], $start: Float, $end: Float, $actor: Int) {
  reportData { report(code: $code) {
    taken: events(fightIDs: $fightIDs, startTime: $start, endTime: $end, sourceID: $actor,
                  dataType: DamageTaken, includeResources: true, limit: 1000) { data }
    healed: events(fightIDs: $fightIDs, startTime: $start, endTime: $end, targetID: $actor,
                   dataType: Healing, includeResources: true, limit: 1000) { data }
  } }
}
"""


def load_defensives() -> dict[str, dict]:
    if not DEFENSIVES_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(DEFENSIVES_PATH.read_text()).items() if not k.startswith("_")}


def fetch_group_casts(client: WCLClient, fight: Fight, player_ids: set[int]) -> list[list]:
    """Every player's casts: [t, source, ability id, target]. Cached; the defensive list can change later."""
    key = f"groupcasts:v{GROUP_CASTS_VERSION}:{fight.report_code}:{fight.id}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    rows = [[e["timestamp"], e["sourceID"], e.get("abilityGameID", 0), e.get("targetID")]
            for e in _events(client, fight, "Casts", hostility="Friendlies")
            if e.get("type") == "cast" and e.get("sourceID") in player_ids]
    client.set_cached(key, rows)
    return rows


SCHOOLS_QUERY = """
query($code: String!) { reportData { report(code: $code) { masterData { abilities { gameID type } } } } }
"""
PHYSICAL = 1  # WCL ability type is the spell-school bitmask; 1 = physical


def ability_schools(client: WCLClient, fight: Fight) -> dict[int, int]:
    data = client.query(SCHOOLS_QUERY, {"code": fight.report_code})["reportData"]["report"]["masterData"]
    out = {}
    for a in data["abilities"]:
        try:
            out[a["gameID"]] = int(a.get("type") or 0)
        except (TypeError, ValueError):
            out[a["gameID"]] = 0
    return out


def _school_ok(effect: dict, school: int) -> bool:
    """Effects limited to magic or physical damage only apply to hits of that school."""
    limit = effect.get("school")
    if not limit or not school:
        return True
    return (school == PHYSICAL) == (limit == "physical")


def fetch_death_events(client: WCLClient, fight: Fight, victim: int, t: int) -> dict:
    key = f"deathevents:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{victim}:{t}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    report = client.query(DEATH_EVENTS_QUERY, {"code": fight.report_code, "fightIDs": [fight.id],
                                               "start": t - WINDOW_MS, "end": t + 1, "actor": victim}, ttl=0)
    report = report["reportData"]["report"]
    out = {
        "damage": [[e["timestamp"], e.get("abilityGameID", 0), e.get("amount", 0), e.get("absorbed") or 0,
                    e.get("overkill") or 0, e.get("hitPoints"), e.get("maxHitPoints")]
                   for e in report["taken"]["data"] if e.get("type") == "damage" and e.get("targetID") == victim],
        "heals": [[e["timestamp"], e.get("abilityGameID", 0), e.get("sourceID"), e.get("amount", 0),
                   e.get("overheal") or 0, e.get("hitPoints"), e.get("maxHitPoints"), e["type"] == "absorbed"]
                  for e in report["healed"]["data"] if e.get("type") in ("heal", "absorbed")],
    }
    client.set_cached(key, out)
    return out


@dataclass
class Replay:
    died: bool
    lowest: float  # lowest HP as share of max


def replay(ev: dict, t_death: int, effect: dict | None, duration_s: float,
           schools: dict[int, int] | None = None, dots: dict[int, int] | None = None) -> Replay:
    """What-if on the victim's last seconds. It follows the real HP after every hit and heal
    and only tracks the *extra* HP the defensive would have given (damage prevented, shield,
    heal, max HP), applied as if pressed just in time: active for `duration_s` ending at the
    death. With no effect it reproduces the real death exactly. `dots`: debuff id -> when it
    landed, so a deflect doesn't stop ticks of a DoT that was already on the player."""
    stream = [(d[0], 0, "dmg", d) for d in ev["damage"]] + [(h[0], 1, "heal", h) for h in ev["heals"] if not h[7]]
    stream.sort(key=lambda x: (x[0], x[1]))
    if not stream:
        return Replay(True, 0.0)
    max_hp = next((r[3][6] for r in stream if r[3][6]), None) or 1
    effect = effect or {}
    t_on = max(stream[0][0], t_death - duration_s * 1000)
    cap, extra, shield, applied, last_t = float(max_hp), 0.0, 0.0, not effect, stream[0][0]
    actual = None
    lowest = 1.0
    for ts, _, kind, row in stream:
        if not applied and ts >= t_on:
            applied = True
            cap += max_hp * effect.get("max_hp", 0)
            extra += max_hp * (effect.get("max_hp", 0) + effect.get("heal", 0))
            shield += max_hp * effect.get("absorb", 0)
        active = bool(effect) and applied and ts <= t_on + duration_s * 1000
        if active and effect.get("hps"):
            extra += max_hp * effect["hps"] * (ts - max(last_t, t_on)) / 1000
        last_t = ts
        if kind == "dmg":
            _, aid, amount, absorbed, overkill, hp, _ = row
            lost = amount + overkill
            prevented = 0.0
            covers = active and _school_ok(effect, (schools or {}).get(aid, 0))
            ticking = aid in (dots or {}) and dots[aid] < t_on  # applied before the deflect went up
            if covers and (effect.get("immune") or (effect.get("deflect") and not ticking)):
                prevented = lost
            elif covers and effect.get("dr"):
                prevented = min(lost, (lost + absorbed) * effect["dr"])
            soak = min(shield, lost - prevented) if covers or not effect.get("school") else 0.0
            shield -= soak
            extra += prevented + soak
            actual = hp if hp is not None else max(0.0, (actual if actual is not None else max_hp) - amount)
            what_if = actual + extra - (overkill if actual <= 0 else 0)
        else:
            hp = row[5]
            actual = hp if hp is not None else min(max_hp, (actual if actual is not None else max_hp) + row[3])
            what_if = actual + extra
        what_if = min(cap, what_if)
        extra = max(0.0, what_if - actual)  # HP above the cap is lost
        lowest = min(lowest, what_if / max_hp)
        if what_if <= 0:
            return Replay(True, 0.0)
    return Replay(False, max(0.0, lowest))


def _applies(d: dict, p: Player) -> bool:
    return d["class"] in ("*", p.class_name) and ("spec" not in d or d["spec"] == p.spec)


def _pulses(run: RunData, victim: int, pull) -> set[int]:
    """Abilities that hit the victim small and often for the whole pull - background, not a cause."""
    if not pull:
        return set()
    span_s = max(1.0, (pull.end - pull.start) / 1000)
    hits: dict[int, list[list]] = {}
    for d in run.damage:
        if d[1] == victim and pull.start <= d[0] <= pull.end:
            hits.setdefault(d[2], []).append(d)
    out = set()
    for aid, rows in hits.items():
        covered = (rows[-1][0] - rows[0][0]) / 1000 / span_s
        mx = max((r[6] or 0) for r in rows) or 1
        typical = sorted((r[3] + r[4]) / mx for r in rows)[len(rows) // 2]
        if covered >= PULSE_COVERAGE and len(rows) >= span_s / PULSE_EVERY_S and typical <= PULSE_MAX_HIT:
            out.add(aid)
    return out


def _cause(ev: dict, name_of, pulses: set[int]) -> dict:
    """What actually killed them: damage share over the window, not the killing blow."""
    by: dict[int, float] = {}
    for _, aid, amount, absorbed, overkill, _, _ in ev["damage"]:
        by[aid] = by.get(aid, 0) + amount + absorbed + overkill
    total = sum(by.values()) or 1
    ranked = sorted(by.items(), key=lambda x: -x[1])
    main = next(((a, v) for a, v in ranked if a not in pulses and v / total >= CAUSE_MIN_SHARE), ranked[0] if ranked else (0, 0))
    return {
        "ability": main[0], "name": name_of(main[0]), "share": round(main[1] / total, 2),
        "lines": [f"{name_of(a)}: {v / total:.0%} of the damage in the last 10s"
                  + (" (constant pulse all pull - background)" if a in pulses else "")
                  for a, v in ranked[:3] if v / total >= 0.02],
        "shares": {name_of(a): round(v / total, 2) for a, v in ranked},  # by name: debuffs and their damage match by name
    }


def _used_on(run: RunData, victim: int, ts: int, auras: Auras | None) -> str:
    """What was hurting the player when they used a cooldown - "during Bloodthorn Roots"."""
    if auras:
        # A debuff that had just landed, not a long-running one like Exhaustion/Sated.
        on = [d for d in auras.on_at(victim, ts) if 1500 < d[3] - d[2] <= 60_000 and d[2] >= ts - 15_000]
        if on:
            return f" during {run.ability_name(max(on, key=lambda d: d[2])[1])}"
    near: dict[int, float] = {}
    for d in run.damage:
        if d[1] == victim and ts - 3000 <= d[0] <= ts + 1000:
            near[d[2]] = near.get(d[2], 0) + d[3] + d[4]
    return f" during {run.ability_name(max(near, key=near.get))}" if near else ""


def recap_death(run: RunData, players: list[Player], group_casts: list[list], defensives: dict,
                ev: dict, t: int, victim_id: int, killing: int, schools: dict[int, int] | None = None,
                auras: Auras | None = None, can_dispel: set[int] | None = None,
                self_clears: dict[int, set[str]] | None = None, top_handling: dict[str, str] | None = None,
                procs: dict[str, list[list]] | None = None, own_dispel: set[int] | None = None,
                totems: dict[int, str] | None = None, stored: list[tuple] | None = None) -> dict:
    by_id = {p.id: p for p in players}
    victim = by_id.get(victim_id)
    rel = lambda ts: round((ts - t) / 1000, 1)
    name_of = run.ability_name
    clock = lambda ts: f"{(ts - run.start) // 60000}:{(ts - run.start) // 1000 % 60:02d}"
    pname = lambda pid: by_id[pid].name if pid in by_id else "totem/pet"
    baseline = replay(ev, t, None, 0)
    pull = next((p for p in run.pulls if p.start <= t <= p.end), None)
    cause = _cause(ev, name_of, _pulses(run, victim_id, pull))

    casts_by: dict[tuple[int, str], list[int]] = {}
    for c in group_casts:
        casts_by.setdefault((c[1], name_of(c[2])), []).append(c[0])
    dots: dict[int, int] = {}
    for d in (auras.debuffs if auras else []):
        if d[0] == victim_id and d[2] <= t and d[3] >= t - WINDOW_MS:
            dots[d[1]] = min(dots.get(d[1], d[2]), d[2])

    ready: dict[str, list[dict]] = {"group": [], "victim": [], "healer": []}
    for p in players:
        for name, d in defensives.items():
            if not _applies(d, p):
                continue
            scope = d["scope"]
            if (scope == "personal" and p.id != victim_id) or (scope == "external" and p.id == victim_id):
                continue
            # Personals are the victim's; the healer's group/external cooldowns are the healer's even
            # when the healer is the one who died.
            bucket = "victim" if scope == "personal" else "healer" if p.id == run.healer_id else "group"
            if not casts_by.get((p.id, name)) and casts_by.get((p.id, d.get("choice"))):
                continue  # the other talent of a choice node was pressed: this one isn't talented
            times = uses(casts_by.get((p.id, name), []), max(SAME_USE_MS, d["duration"] * 1000))
            charges = infer_charges(times, d["cd"] * 1000)
            cd_ms = learn_recharge_ms(times, d["cd"], charges)
            item = {"name": name, "owner": p.name, "scope": scope, "gcd": bool(d.get("gcd"))}
            used = [x for x in times if t - d["duration"] * 1000 <= x <= t]
            last = max((x for x in times if x <= t), default=None)
            wait = wait_ms(times, t, cd_ms, charges)
            two = f", {charges} charges" if charges > 1 else ""
            if used:
                item.update(status="used", text=f"Used {(t - used[-1]) / 1000:.0f}s before the death - it was up.")
            elif not times:
                item.update(status="unknown", text="Not pressed all run - may not be talented.")
            elif last is None:
                item.update(status="ready", text="Ready - not pressed yet at that point (used later in the run).")
            elif not wait:
                item.update(status="ready", text=f"Ready (last used {(t - last) / 1000:.0f}s earlier{two}).")
            else:
                # Say why it wasn't available: when it was used and on what.
                item.update(status="cooldown", text=f"Used at {clock(last)} ({(t - last) / 1000:.0f}s earlier"
                            f"{_used_on(run, p.id, last, auras)}{two}) - back {wait / 1000:.0f}s after the death.")
            if d["effect"].get("escape"):
                item["escape"] = True
            elif item["status"] in ("ready", "unknown"):
                r = replay(ev, t, d["effect"], d["duration"], schools, dots)
                item["verdict"] = "died" if r.died else "close" if r.lowest < LIVED_MIN else "lived"
                item["lowest"] = round(r.lowest, 2)
            ready[bucket].append(item)
    # Stored heals (Stormstream Totem): kept at the death, used just before it, or none stored.
    for name, spans, effect, duration in stored or []:
        item = {"name": name, "owner": pname(run.healer_id), "scope": "group", "gcd": True}
        now = next((s for s in spans if s[0] <= t <= s[1]), None)
        used = [s[1] for s in spans if t - WINDOW_MS <= s[1] < t]
        last = max((s[1] for s in spans if s[1] < t), default=None)
        if now:
            item.update(status="ready", text=f"Stored since {clock(now[0])} ({(t - now[0]) / 1000:.0f}s) and unused.")
            r = replay(ev, t, effect, duration, schools, dots)
            item["verdict"] = "died" if r.died else "close" if r.lowest < LIVED_MIN else "lived"
            item["lowest"] = round(r.lowest, 2)
        elif used:
            item.update(status="used", text=f"Used {(t - used[-1]) / 1000:.0f}s before the death.")
        elif last is not None:
            item.update(status="cooldown", text=f"None stored - the last one was used at {clock(last)}.")
        elif spans:
            item.update(status="cooldown", text="None stored yet at that point.")
        else:
            continue  # never stored this run: not talented
        ready["healer"].append(item)
    # Passive cheat-death procs already spent (e.g. Nature's Guardian on the healer).
    if victim_id == run.healer_id:
        for name, spans in (procs or {}).items():
            before = [s for s in spans if s[0] <= t]
            if before:
                ready["victim"].append({"name": name, "owner": pname(victim_id), "scope": "personal", "status": "cooldown",
                                        "text": f"Passive - already procced at {clock(before[-1][0])} "
                                                f"({(t - before[-1][0]) / 1000:.0f}s earlier)."})
    order = {"ready": 0, "unknown": 1, "used": 2, "cooldown": 3}
    for items in ready.values():
        items.sort(key=lambda i: (order[i["status"]], {"lived": 0, "close": 1, "died": 2}.get(i.get("verdict"), 3)))

    rows = []
    for ts, aid, amount, absorbed, overkill, hp, mx in ev["damage"]:
        school = (schools or {}).get(aid, 0)
        rows.append({"dt": rel(ts), "kind": "hit", "ability": name_of(aid), "amount": amount + overkill,
                     "school": "" if not school else "physical" if school == PHYSICAL else "magic",
                     "absorbed": absorbed, "overkill": overkill, "hp": round(hp / mx, 2) if hp is not None and mx else None})
    for ts, aid, src, amount, overheal, hp, mx, is_absorb in ev["heals"]:
        if is_absorb:
            continue
        rows.append({"dt": rel(ts), "kind": "heal", "ability": name_of(aid),
                     "source": by_id[src].name if src in by_id else "totem/pet",
                     "amount": amount, "overheal": overheal, "hp": round(hp / mx, 2) if hp is not None and mx else None})
    for c in group_casts:
        nm = name_of(c[2])
        if t - WINDOW_MS <= c[0] <= t and nm in defensives:
            rows.append({"dt": rel(c[0]), "kind": "defensive", "ability": nm, "source": pname(c[1])})
    rows.sort(key=lambda r: r["dt"])
    taken = sum(r["amount"] for r in rows if r["kind"] == "hit")
    healed = sum(r["amount"] for r in rows if r["kind"] == "heal")

    # What the dying player was doing: their own casts in the last seconds, with their HP then.
    hp_marks = sorted([(r[0], r[5] / r[6]) for r in ev["damage"] if r[5] is not None and r[6]]
                      + [(h[0], h[5] / h[6]) for h in ev["heals"] if h[5] is not None and h[6] and not h[7]])
    last_casts = []
    for c in group_casts:
        if c[1] == victim_id and t - LAST_CASTS_MS <= c[0] <= t:
            hp = next((v for ts, v in reversed(hp_marks) if ts <= c[0]), None)
            tgt = c[3]
            last_casts.append({"dt": rel(c[0]), "ability": name_of(c[2]),
                               "target": "self" if tgt == victim_id else pname(tgt) if tgt in by_id else "",
                               "hp": round(hp, 2) if hp is not None else None})

    story = _debuff_story(run, players, group_casts, defensives, auras, can_dispel or set(), self_clears or {},
                          top_handling or {}, t, victim_id, cause, clock, pname, own_dispel, totems or {}) if auras else None
    return {
        "t": t, "victim": victim.name if victim else run.player_name(victim_id),
        "victim_spec": victim.label if victim else None, "killing": name_of(killing),
        "taken": taken, "healed": healed, "max_hp": next((r[6] for r in ev["damage"] if r[6]), None),
        "replay_ok": baseline.died, "ready": ready, "events": rows,
        "cause": cause, "story": story, "last_casts": last_casts,
    }


def low_for_s(events: list[dict]) -> float:
    """How long the victim had been under LOW_HP right before dying (0 if the drop came out of nowhere)."""
    marks = sorted((e["dt"], e["hp"]) for e in events if e["kind"] in ("hit", "heal") and e.get("hp") is not None)
    start = None
    for dt, hp in marks:
        if hp >= LOW_HP:
            start = None
        elif start is None:
            start = dt
    return -start if start is not None else 0.0


def judge_reaction(r: dict, telegraphed: str = "") -> None:
    """A ready personal only counts as missed when the victim could react: a telegraphed or known mechanic,
    or a stretch under 50%. Without that window the items get `no_window` (one context line, no weight)."""
    low = low_for_s(r["events"])
    why = telegraphed or (f"under 50% for {low:.1f}s" if low >= LOW_FOR_S else "")
    r["reaction"] = {"window": bool(why), "why": why or f"{r['cause']['name']} wasn't telegraphed and they went "
                     + (f"under 50% only {low:.1f}s before dying" if low else "from above 50% straight to dead")}
    for i in r["ready"]["victim"]:
        i.pop("no_window", None)
        if not why and i["status"] in ("ready", "unknown") and i.get("verdict"):
            i["no_window"] = True


def learn_dispels(sources: list[tuple]) -> tuple[set[int], dict[int, str]]:
    """From (Auras, healer id, name_of) of your run and peer healers' runs: debuffs the healer's own dispel removes,
    and debuffs a cleanse totem removes (debuff -> totem cast name)."""
    own, totems = set(), {}
    for auras, healer, name_of in sources:
        for _, src, _, removed, tool in auras.dispels:
            n = name_of(tool)
            if n in CLEANSE_TOTEMS:
                totems[removed] = CLEANSE_TOTEMS[n][0]
            elif src == healer and n in DISPEL_SPELLS:
                own.add(removed)
    return own, totems


def _totem_story(run, group_casts, totem: str, healer: int, start: int, until: int, debuff: str, clock,
                 used_on) -> tuple[str, str, str]:
    """A cleanse totem that clears this debuff: (line, advice, fix)."""
    cd_s = next(cd for n, cd in CLEANSE_TOTEMS.values() if n == totem)
    times = sorted(c[0] for c in group_casts if c[1] == healer and run.ability_name(c[2]) == totem)
    if not times:
        return f"{totem} clears it - not pressed all run (may not be talented).", "", ""
    used_in = [x for x in times if start - 1000 <= x <= until + 500]
    if used_in:
        return f"{totem} (clears it): dropped at {clock(used_in[0])}.", "", ""
    last = max((x for x in times if x < start), default=None)
    back = free_at(times, start, cd_s)
    if back > start:
        line = f"{totem} (clears it): used at {clock(last)}{used_on(last)}, back {(back - start) / 1000:.0f}s after {debuff} landed."
        if back < until - 1000:
            return line, (f"{totem} came back at {clock(back)} and wasn't dropped - {(until - back) / 1000:.0f}s more "
                          f"in {debuff}."), f"Drop {totem}"
        return line, "", ""
    return (f"{totem} (clears it): ready when {debuff} landed.",
            f"{totem} was ready and clears {debuff} - drop it as it lands.", f"Drop {totem}")


def clear_tools(defensives: dict) -> set[str]:
    """Abilities that can end a debuff on their user: escapes and immunities (not plain damage reduction)."""
    return {n for n, d in defensives.items() if d["effect"].get("escape") or d["effect"].get("immune")}


def _spent_tools(run, group_casts, defensives, known: set[str], victim_id: int, start: int, until: int) -> list[str]:
    """The victim's own clear tools for this debuff that stayed on cooldown the whole time it was up."""
    out = []
    for n in sorted(known):
        d = defensives.get(n)
        if not d:
            continue
        times = [c[0] for c in group_casts if c[1] == victim_id and run.ability_name(c[2]) == n]
        if free_at(times, start, d["cd"]) > until - 1000:
            out.append(n)
    return out


def _debuff_story(run, players, group_casts, defensives, auras: Auras, can_dispel, self_clears, top_handling,
                  t, victim_id, cause, clock, pname, own_dispel: set[int] | None = None,
                  totems: dict[int, str] | None = None) -> dict | None:
    """The debuff behind the death: how long it sat on the victim, who it hit, how each of
    them got rid of it, where the dispels went, and which escape tools were still up."""
    name_of = run.ability_name
    spans = [d for d in auras.on_at(victim_id, t - 200)] + [d for d in auras.debuffs if d[0] == victim_id
                                                           and t - WINDOW_MS <= d[3] <= t + 200]
    if not spans:
        return None
    # The cause's debuff if there is one, else the longest dispellable debuff they had.
    # Only a debuff that mattered for the death: the cause itself, an interruptible cast that took control away
    # (Seduction - the answer is the kick), or one that
    # did a real share of the damage (Bloodthorn Roots). A stacking debuff that happened to be on them (Cold Claws while
    # melee killed them) is not the story, however dispellable.
    mattered = lambda a: (name_of(a) == cause["name"] or (FACTS.loss_of_control(a) and FACTS.interruptible(a))
                          or cause.get("shares", {}).get(name_of(a), 0) >= CAUSE_MIN_SHARE)
    clearable = can_dispel | set(totems or {})
    spans = sorted({tuple(s) for s in spans if mattered(s[1])},
                   key=lambda s: (name_of(s[1]) != cause["name"], s[1] not in clearable, s[2] - s[3]))
    if not spans:
        return None
    tgt, aid, start, end = spans[0]
    debuff = name_of(aid)
    dispellable = aid in can_dispel
    # Your own dispel only removes some types (a Resto Shaman's doesn't touch poison): learned from your and peer
    # healers' dispels. Unknown (no data passed) = trust "somebody dispelled it".
    mine = dispellable if own_dispel is None else aid in own_dispel
    loc = FACTS.loss_of_control(aid)  # charmed / stunned / feared: the victim can't act, so can't dispel themselves
    if loc and victim_id == run.healer_id:
        mine = False
    healer_class = next((p.class_name for p in players if p.id == run.healer_id), "")
    totem = (totems or {}).get(aid) if healer_class == "Shaman" else None  # only Shamans have cleanse totems
    tools = clear_tools(defensives)
    lines, advice, fixes = [], [], []
    on_for = (min(end, t) - start) / 1000
    lines.append(f"{debuff} was on {pname(victim_id)} from {clock(start)} - {on_for:.1f}s "
                 + ("until the death." if end >= t - 300 else f"(removed {(t - end) / 1000:.1f}s before the death)."))
    if loc and FACTS.interruptible(aid):  # the answer is stopping the cast, not anything the victim does
        advice.append(f"{debuff} is a {loc} effect - {pname(victim_id)} can't break it themselves. It's an interruptible "
                      f"cast: interrupt or stun the caster before it lands.")
        fixes.append(f"Interrupt {debuff}")

    wave = [d for d in auras.debuffs if d[1] == aid and abs(d[2] - start) <= WAVE_MS and d[0] != victim_id]
    for d in wave:
        how = removed_by(auras, d, group_casts, name_of, tools)
        if how.startswith("dispel:"):
            who = f"dispelled by {pname(int(how.split(':')[1]))}"
        elif how == "self-dispel":
            who = "dispelled themselves"
        elif how == "expired":
            who = "it dropped off with no dispel or known escape seen"
        else:
            who = f"cleared it themselves with {how}"
        lines.append(f"Also hit {pname(d[0])}: {who} after {(d[3] - d[2]) / 1000:.1f}s.")

    healer = run.healer_id
    healer_dispels = [x for x in auras.dispels if x[1] == healer]
    dispel_ids = {x[4] for x in healer_dispels}
    gaps = sorted(b[0] - a[0] for a, b in zip(healer_dispels, healer_dispels[1:]))
    dispel_cd = gaps[0] if gaps else 8000
    in_window = [x for x in healer_dispels if start <= x[0] <= t + 200]
    if dispellable and not mine:
        lines.append(f"{debuff} can be dispelled by someone else - not by you while it holds you." if loc else
                     f"{debuff} can be dispelled, but not with your dispel" + (f" - {totem} clears it." if totem else "."))
    if totem:
        t_line, t_advice, t_fix = _totem_story(run, group_casts, totem, healer, start, min(end, t), debuff, clock,
                                               lambda ts: _used_on(run, healer, ts, auras))
        lines.append(t_line)
        if t_advice:
            advice.append(t_advice)
            fixes.append(t_fix)
    if mine:
        if in_window:
            lines.append("Your dispels while it was up: " + "; ".join(
                f"{clock(x[0])} on {'yourself' if x[2] == healer else pname(x[2])} ({name_of(x[3])})" for x in in_window) + ".")
        else:
            lines.append("No dispel from you while it was up.")
        if victim_id == healer:
            others_first = [x for x in in_window if x[2] != healer and x[3] == aid]
            if others_first:
                late = (min(end, t) - others_first[0][0]) / 1000
                spent = _spent_tools(run, group_casts, defensives, self_clears.get(aid, set()), victim_id, start, min(end, t))
                lead = f"With {' and '.join(spent)} on cooldown, dispel yourself first" if spent else "Dispel yourself first"
                advice.append(f"{lead}: you dispelled {pname(others_first[0][2])} at {clock(others_first[0][0])} "
                              f"and stayed in {debuff} for another {late:.1f}s; your dispel was back only "
                              f"{dispel_cd / 1000:.0f}s later.")
                fixes.append("Dispel yourself first")
            elif not any(x[2] == healer and x[3] == aid for x in in_window) and on_for >= SELF_DISPEL_LATE_S:
                spent = _spent_tools(run, group_casts, defensives, self_clears.get(aid, set()), victim_id, start, min(end, t))
                lead = f"With {' and '.join(spent)} on cooldown, dispel" if spent else "Dispel"
                advice.append(f"{lead} yourself as soon as {debuff} lands - you had it for {on_for:.1f}s "
                              "without dispelling.")
                fixes.append("Dispel yourself as it lands")
        elif not in_window:
            advice.append(f"{debuff} can be dispelled - {pname(victim_id)} had it for {on_for:.1f}s "
                          "with no dispel on them.")
            fixes.append(f"Dispel {pname(victim_id)}")
        elif not any(x[2] == victim_id for x in in_window):
            went = "; ".join(f"{'yourself' if x[2] == healer else pname(x[2])} at {clock(x[0])}" for x in in_window)
            advice.append(f"Your dispel went to {went}, while {pname(victim_id)} sat in {debuff} for {on_for:.1f}s and "
                          f"died - pick whoever can't clear it themselves and is lowest.")
            fixes.append(f"Dispel {pname(victim_id)} instead")

    # Victim's own ways out: only tools seen clearing this debuff (this run or the top logs).
    victim = next((p for p in players if p.id == victim_id), None)
    known = self_clears.get(aid, set())
    options = []
    for n, d in defensives.items():
        if victim and d["class"] in ("*", victim.class_name) and ("spec" not in d or d["spec"] == victim.spec) \
                and n in known and d["scope"] == "personal":
            times = sorted(c[0] for c in group_casts if c[1] == victim_id and name_of(c[2]) == n)
            last = max((x for x in times if x <= start), default=None)
            back = free_at(times, start, d["cd"])
            used_in = [x for x in times if start <= x <= min(end, t) + 500]
            if used_in:
                options.append(f"{n} (clears it): used at {clock(used_in[0])}")
            elif back > start:
                options.append(f"{n} (clears it): used at {clock(last)}{_used_on(run, victim_id, last, auras)}, "
                               f"back {(back - start) / 1000:.0f}s after {debuff} landed")
                if back < min(end, t) - 1000:
                    advice.append(f"{n} came back at {clock(back)}, {(back - start) / 1000:.0f}s after {debuff} landed, "
                                  f"and wasn't used - {(min(end, t) - back) / 1000:.0f}s more in it. Use it to break out.")
                    fixes.append(f"{n} out of it")
            else:
                options.append(f"{n} (clears it): ready when {debuff} landed")
                advice.append(f"{n} was ready when {debuff} landed and clears it - use it straight away.")
                fixes.append(f"{n} out of it")
    if mine and victim_id == healer:
        options.append("your own dispel")
    if options:
        lines.append("Your ways out: " + "; ".join(options) + "." if victim_id == healer
                     else f"{pname(victim_id)}'s ways out: " + "; ".join(options) + ".")

    # The previous time it hit them: what did it cost?
    prev = [d for d in auras.spans(victim_id, aid) if d[3] < start]
    if prev:
        p_how = removed_by(auras, prev[-1], group_casts, name_of, tools)
        if p_how not in ("expired", "self-dispel") and not p_how.startswith("dispel:"):
            lines.append(f"The previous {debuff} (at {clock(prev[-1][2])}) was cleared with {p_how}.")
            if mine and victim_id == healer and p_how in _spent_tools(
                    run, group_casts, defensives, {p_how}, victim_id, start, min(end, t)):
                advice.append(f"On the previous {debuff} you used {p_how}; dispelling yourself there would have kept "
                              f"{p_how} for this one.")
    # Players in the wave who can clear it themselves, from what they did before.
    for d in wave:
        for other in reversed(auras.spans(d[0], aid)):  # most recent example first
            how = removed_by(auras, other, group_casts, name_of, tools)
            if how == "expired" or how.startswith("dispel:") or other[2] >= start:
                continue
            if mine:
                if d[0] == healer:
                    advice.append(f"You can clear it yourself with {'your dispel' if how == 'self-dispel' else how} "
                                  f"(did at {clock(other[3])}).")
                else:
                    method = "their own dispel" if how == "self-dispel" else how
                    advice.append(f"{pname(d[0])} can clear it themselves with {method} (did at {clock(other[3])}) - "
                                  f"your dispel can go elsewhere.")
            break
    # How peers got rid of it only means something when it can be got rid of (Hearty Bellow can't: it just expires).
    if debuff in top_handling and (dispellable or totem or known):
        lines.append(top_handling[debuff] + f" ({pname(victim_id)}: {on_for:.1f}s.)")
    return {"debuff": debuff, "dispellable": dispellable, "on_for": round(on_for, 1), "lines": lines, "advice": advice,
            "fix": fixes[0] if fixes else ""}
