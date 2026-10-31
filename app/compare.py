"""You vs one top player, pull by pull: what they did differently, whether it mattered,
and whether the difference came from healing, from damage taken, or from the route/comp."""
import statistics

from app.auras import fetch_auras
from app.avoidable import cached as cached_avoidance
from app.composition import intake_modifiers, load_effects
from app.context import CooldownTracker
from app.deaths import load_defensives
from app.defense_spans import fetch_defense_spans
from app.healer_state import fetch_healer_state
from app.metrics import riptide_active
from app.reports import get_fights, get_players, get_trinkets, parse_report_url, pick_fight
from app.runs import fetch_run
from app.segments import all_segments
from app.specs import resto_shaman as rs
from app.spells import fetch_spell_heals, heal_casts
from app.timeline import build_timeline
from app.wcl import WCLClient

SAME_SCORE = 10  # below this a pull "played out the same"
HP_GAP, DAMAGE_RATIO, DURATION_RATIO = 0.05, 1.25, 1.4
CD_TIMING_S, RIPTIDE_GAP, CH_GAP = 20, 1.0, 0.2


class Side:
    """Everything one run needs for the comparison."""

    def __init__(self, client: WCLClient, url: str, player_name: str | None):
        code, fid = parse_report_url(url)
        self.fight = pick_fight(get_fights(client, code), fid)
        self.players = get_players(client, self.fight)
        shamans = [p for p in self.players if p.class_name == rs.CLASS_NAME and p.spec == rs.SPEC_NAME]
        named = [p for p in shamans if player_name and p.name.lower() == player_name.lower()]
        if not (named or shamans):
            raise ValueError("No Restoration Shaman found in that run.")
        self.me = (named or shamans)[0]
        self.run = fetch_run(client, self.fight, self.me.id)
        self.tl = build_timeline(self.run)
        self.segs = all_segments(self.run, self.tl)
        state = fetch_healer_state(client, self.fight, self.run)
        self.heals = heal_casts(self.run, self.tl, fetch_spell_heals(client, self.fight, self.run), state)
        self.auras = fetch_auras(client, self.fight, self.run.player_ids)
        self.spans = fetch_defense_spans(client, self.fight, self.run, self.players, load_defensives())
        self.trinkets = get_trinkets(client, self.fight).get(self.me.id, [])
        self.mods = intake_modifiers(self.players, load_effects())
        self.riptide = riptide_active(self.run, self.tl)
        self.cds = CooldownTracker(self.run)
        self.avoid = cached_avoidance(client, code, self.fight.id)  # set when this run was analyzed

    def pull_stats(self, seg) -> dict:
        p, run = seg.pull, self.run
        inside = lambda t: p.start <= t <= p.end
        rel = lambda t: round((t - p.start) / 1000)
        name = run.ability_name
        tools = {**rs.HEALING_COOLDOWNS, **rs.PERSONAL_COOLDOWNS}
        cds = [(name(a), rel(t)) for t, a, _ in run.casts if inside(t) and (a in tools or name(a) == "Spirit Walk")]
        items = [name(a) for t, a, _ in run.casts if inside(t) and (name(a) in self.trinkets or "Potion" in name(a))]
        rip = [self.riptide[max(0, self.tl.bin_of(h.t) - 1)] for h in seg.hits]
        busy = [h for h in self.heals if inside(h.t) and h.injured >= 2]
        dispels = [d for d in self.auras.dispels if d[1] == self.me.id and inside(d[0])]
        avoid, total, names = self.avoid.damage_in(run, p.start, p.end) if self.avoid else (0.0, 0.0, {})
        return {
            "avoidable": round(avoid / total, 3) if total else None,
            "avoidable_names": [n for n, _ in sorted(names.items(), key=lambda x: -x[1])[:3]],
            "duration_s": round(seg.duration_s), "group_hp": round(seg.median_ehp, 3),
            "deaths": sum(1 for d in run.deaths if inside(d[0])), "intake": seg.intake_frac_ps,
            "cooldowns": cds, "items": items,
            "riptides_before_hits": round(statistics.mean(rip), 1) if rip else None,
            "ch_share_busy": round(sum(1 for h in busy if h.spell == "Chain Heal") / len(busy), 2) if len(busy) >= 8 else None,
            "dispels": [("self" if d[2] == self.me.id else "other", name(d[3]), rel(d[0])) for d in dispels],
            "group_defensives": sorted({s["name"] for s in self.spans if s["scope"] == "group" and inside(s["start"])}),
        }


def compare_runs(client: WCLClient, my_url: str, other_url: str, my_name: str | None) -> dict:
    mine, theirs = Side(client, my_url, my_name), Side(client, other_url, None)
    verdicts, same, unmatched = [], 0, 0
    pairs = _pair_pulls(mine.segs, theirs.segs)
    for i, seg in enumerate(mine.segs):
        if seg.duration_s < 10:
            continue
        oseg = pairs.get(id(seg))
        if not oseg:
            unmatched += 1
            continue
        a, b = mine.pull_stats(seg), theirs.pull_stats(oseg)
        v = _verdict(seg.pull.name + (" (boss)" if seg.pull.is_boss else ""), i, a, b, mine, theirs)
        if v["score"] < SAME_SCORE:
            same += 1
        else:
            verdicts.append(v)
    verdicts.sort(key=lambda v: -v["score"])
    return {
        "you": {"player": mine.me.name, "key": mine.fight.keystone_level, "timed": bool(mine.fight.kill),
                "duration_min": round(mine.fight.duration_s / 60, 1), "deaths": len(mine.run.deaths)},
        "them": {"player": theirs.me.name, "key": theirs.fight.keystone_level, "timed": bool(theirs.fight.kill),
                 "duration_min": round(theirs.fight.duration_s / 60, 1), "deaths": len(theirs.run.deaths),
                 "url": other_url},
        "verdicts": verdicts, "same": same, "unmatched": unmatched,
    }


def _pair_pulls(mine: list, theirs: list) -> dict:
    """Bosses by encounter; trash by lead mob name, k-th occurrence to k-th occurrence -
    enemy sets alone pair different-sized packs."""
    out, used = {}, set()
    for seg in mine:
        p = seg.pull
        if p.is_boss:
            cand = [s for s in theirs if s.pull.encounter_id == p.encounter_id]
        else:
            k = sum(1 for s in mine if s is not seg and not s.pull.is_boss and s.pull.name == p.name
                    and s.pull.start < p.start)
            same = [s for s in theirs if not s.pull.is_boss and s.pull.name == p.name]
            cand = same[k:k + 1]
        cand = [s for s in cand if id(s) not in used]
        if cand:
            out[id(seg)] = cand[0]
            used.add(id(cand[0]))
    return out


def _verdict(name: str, idx: int, a: dict, b: dict, mine: Side, theirs: Side) -> dict:
    """One pull: outcome difference, what caused it, and what they did differently as healer."""
    outcome, healer, why = [], [], []
    if a["deaths"] != b["deaths"]:
        outcome.append(f"you lost {a['deaths']}, they lost {b['deaths']}")
    hp_gap = b["group_hp"] - a["group_hp"]
    if abs(hp_gap) >= HP_GAP:
        outcome.append(f"group HP {a['group_hp']:.0%} vs their {b['group_hp']:.0%}")
    dmg = a["intake"] / b["intake"] if b["intake"] else 1.0
    if dmg >= DAMAGE_RATIO:
        why.append(f"your group took {dmg - 1:.0%} more damage" + (
            f" ({a['avoidable']:.0%} of yours avoidable: {', '.join(a['avoidable_names'])})"
            if a.get("avoidable") and a["avoidable"] >= 0.05 else ""))
    elif dmg <= 1 / DAMAGE_RATIO:
        why.append(f"their group took {1 / dmg - 1:.0%} more damage")
    if mine.mods != theirs.mods:
        why.append("different comp: " + (", ".join(sorted(mine.mods)) or "no damage-reduction spec") + " vs "
                   + (", ".join(sorted(theirs.mods)) or "none"))
    dur = a["duration_s"] / b["duration_s"] if b["duration_s"] else 1.0
    if dur >= DURATION_RATIO or dur <= 1 / DURATION_RATIO:
        why.append(f"pull length {a['duration_s']}s vs {b['duration_s']}s (route/damage)")

    their_cds, my_cds = {}, {}
    for n, t in b["cooldowns"]:
        their_cds.setdefault(n, []).append(t)
    for n, t in a["cooldowns"]:
        my_cds.setdefault(n, []).append(t)
    for n, ts in their_cds.items():
        if n not in my_cds:
            healer.append(f"they pressed {n} ({_rel(ts)}), you didn't")
        elif abs(ts[0] - my_cds[n][0]) >= CD_TIMING_S:
            healer.append(f"{n}: they pressed it {_rel(ts[:1])}, you {_rel(my_cds[n][:1])}")
    if a["riptides_before_hits"] is not None and b["riptides_before_hits"] is not None \
            and b["riptides_before_hits"] - a["riptides_before_hits"] >= RIPTIDE_GAP:
        healer.append(f"{b['riptides_before_hits']:g} Riptides out before big hits vs your {a['riptides_before_hits']:g}")
    if a["ch_share_busy"] is not None and b["ch_share_busy"] is not None and abs(b["ch_share_busy"] - a["ch_share_busy"]) >= CH_GAP:
        healer.append(f"with 2+ injured they Chain Healed {b['ch_share_busy']:.0%} of the time, you {a['ch_share_busy']:.0%}")
    mine_self = [d for d in a["dispels"] if d[0] == "self"]
    their_self = [d for d in b["dispels"] if d[0] == "self"]
    if their_self and not mine_self and a["dispels"]:
        healer.append(f"they dispelled themselves ({their_self[0][1]}); your dispels went to others")
    if len(b["items"]) - len(a["items"]) >= 1:
        healer.append(f"{len(b['items'])} trinket/potion uses vs your {len(a['items'])}")
    if set(b["group_defensives"]) - set(a["group_defensives"]):
        why.append("their group used " + ", ".join(sorted(set(b["group_defensives"]) - set(a["group_defensives"]))))

    worse = a["deaths"] > b["deaths"] or hp_gap >= HP_GAP
    # Only pulls where you did worse are worth a verdict; equal or better ones count as "same".
    score = (30 * max(0, a["deaths"] - b["deaths"]) + 100 * max(0.0, hp_gap) + 8 * len(healer)) if worse else 0
    # What excuses you: your group taking more damage, or a much longer pull (route).
    excuse = dmg >= DAMAGE_RATIO or dur >= DURATION_RATIO
    cause = ("damage taken / route" if excuse and len(healer) <= 1 else
             "healing" if healer and not excuse else "mixed" if healer else "damage taken / route" if excuse else "unclear")
    what = "; ".join(healer[:2]) if healer else "no clear healer difference"
    head = f"{what} - {', '.join(outcome) or 'same outcome'}"
    if why:
        head += f" ({why[0]})"
    return {"pull": name, "index": idx, "score": round(score), "cause": cause, "headline": head,
            "outcome": outcome, "healer": healer, "why": why, "you": a, "them": b}


def _rel(ts: list[int]) -> str:
    """Pull-relative seconds as '22s in' / '2m49s in' (never m:ss - that would read as run time)."""
    return ", ".join((f"{t}s" if t < 120 else f"{t // 60}m{t % 60:02d}s") for t in ts[:3]) + " in"
