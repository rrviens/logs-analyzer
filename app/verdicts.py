"""Plain-language verdicts for the page's data tables: does anything need to change, and
if so what, with example moments from the run. The raw tables stay behind "Show data".

Each section: {"headline": str, "ok": bool, "lines": [{"text": str, "action": bool, "examples": [str]}]}.
"""
from typing import Callable

from app.spells import HealCast
from app.specs.base import Spec

MIN_CASTS = 15  # fewer heals in a situation than this is too little to judge
SHARE_GAP = 0.10  # group-heal share this far from peer healers counts as a different habit
CLEAR_GAIN, SMALL_GAIN = 0.20, 0.05  # per-cast advantage that is worth changing for / that exists at all
EMPTY_HEALS_RATIO = 1.5  # heals into a full group, vs peer healers' share
ITEM_RATE_OK = 0.85
ITEM_READY_S = 60
BUFF_GAP = 0.20
PHASE_GAP = 0.05
PULL_HP_GAP, PULL_INTAKE_RATIO = 0.05, 1.25
MAX_EXAMPLES = 3

SITUATION_LABEL = {"one": "1 player injured", "two": "2 players injured", "group": "3+ players injured"}


def _line(text: str, action: bool = False, examples: list[str] | None = None) -> dict:
    return {"text": text, "action": action, "examples": examples or []}


def _section(lines: list[dict], ok_headline: str, noun: str) -> dict:
    actions = [l for l in lines if l["action"]]
    if not actions:
        return {"headline": ok_headline, "ok": True, "lines": lines}
    head = f"{len(actions)} thing{'s' if len(actions) > 1 else ''} to change in {noun}." if len(actions) > 1 \
        else f"1 thing to change in {noun}: {actions[0]['text'].split(' - ')[0]}"
    return {"headline": head, "ok": False, "lines": lines}


def spell_verdict(choice: dict, casts: list[HealCast], clock: Callable[[int], str], spec: Spec) -> dict:
    if not choice or not choice.get("you") or not spec.spell_pair:
        return {}
    one, grp = spec.spell_pair.single, spec.spell_pair.group
    you, top = choice["you"], choice.get("top") or {}
    lines = []
    for sit, label in SITUATION_LABEL.items():
        y, t = you.get(sit, {}), top.get(sit, {})
        if y.get("casts", 0) < MIN_CASTS or y.get("ch_share") is None or t.get("ch_share") is None:
            continue
        ch, hw = y.get("ch_effective"), y.get("hw_effective")
        gap = y["ch_share"] - t["ch_share"]
        mix = f"you cast {grp} {y['ch_share']:.0%} of the time, peer healers {t['ch_share']:.0%}"
        if abs(gap) < SHARE_GAP:
            lines.append(_line(f"{label}: same mix as peer healers ({mix})."))
            continue
        if not (ch and hw):
            lines.append(_line(f"{label}: {mix}; not enough of one spell to compare which heals more for you."))
            continue
        you_lean, other = (one, grp) if gap < 0 else (grp, one)
        lean_eff, other_eff = (hw, ch) if gap < 0 else (ch, hw)
        gain = other_eff / lean_eff - 1
        if gain < SMALL_GAIN:
            lines.append(_line(f"{label}: you lean on {you_lean} more than peer healers ({mix}) - and that's fine, "
                               f"your {you_lean}s heal as much or more per cast here. Keep doing it."))
        elif gain < CLEAR_GAIN:
            lines.append(_line(f"{label}: you lean on {you_lean} more than peer healers ({mix}); your {other}s heal "
                               f"{gain:.0%} more per cast here, so a little more {other} would help - minor."))
        else:
            examples = _examples([c for c in casts if c.situation == sit and c.spell == you_lean], clock, you_lean)
            lines.append(_line(f"Use {other} more with {label.lower()} - your {other}s heal {gain:.0%} more per "
                               f"cast there, and peer healers pick it more ({mix}).", True, examples))
    empty = _empty_heals(you, top, spec)
    if empty:
        lines.append(empty)
    lines += _buff_lines(choice.get("buffs") or {}, grp)
    return _section(lines, "Spell choice: matches or beats peer healers - no change needed.", "spell choice")


def _examples(casts: list[HealCast], clock, spell: str) -> list[str]:
    """The clearest moments: most players injured, lowest HP first."""
    picks = sorted(casts, key=lambda c: (-c.injured, c.lowest))[:MAX_EXAMPLES]
    return [f"{clock(c.t)}: {c.injured} injured, lowest at {c.lowest:.0%} - you cast {spell}" for c in sorted(picks, key=lambda c: c.t)]


def _empty_heals(you: dict, top: dict, spec: Spec) -> dict | None:
    total = sum(v.get("casts", 0) for v in you.values())
    top_total = sum(v.get("casts", 0) for v in top.values())
    if not total or not top_total:
        return None
    mine, theirs = you["none"]["casts"] / total, top.get("none", {}).get("casts", 0) / top_total
    if theirs and mine >= theirs * EMPTY_HEALS_RATIO and you["none"]["casts"] >= MIN_CASTS:
        return _line(f"Heals into a full group: {mine:.0%} of your heals landed when nobody was injured, vs "
                     f"{theirs:.0%} for peer healers - those casts mostly overheal; use the time for damage" + (f" or {spec.maintenance.name}" if spec.maintenance else "") + ".", True)
    if theirs and mine <= theirs * 0.75:
        return _line(f"Heals into a full group: only {mine:.0%} of your heals, fewer than peer healers ({theirs:.0%}) - good.")
    return _line(f"Heals into a full group: {mine:.0%} of your heals, about the same as peer healers ({theirs:.0%}).")


def _buff_lines(buffs: dict, grp: str) -> list[dict]:
    lines = []
    you, top = buffs.get("you") or {}, buffs.get("top") or {}
    for name, y in you.items():
        t = top.get(name, {})
        if y.get("casts", 0) < MIN_CASTS or y.get("ch_share") is None or t.get("ch_share") is None:
            continue
        gap = t["ch_share"] - y["ch_share"]
        if abs(gap) < BUFF_GAP:
            continue
        more = "more" if gap > 0 else "less"
        # Only advise a switch when your numbers and the peer healers' both say it heals more.
        favours_ch = [s["ch_effective"] > s["hw_effective"] for s in (y, t)
                      if s.get("ch_effective") and s.get("hw_effective")]
        if favours_ch and all(f == (gap > 0) for f in favours_ch):
            lines.append(_line(f"During {name}, {grp} {more} - peer healers do ({t['ch_share']:.0%} vs your "
                               f"{y['ch_share']:.0%}) and it heals more per cast then.", True))
        else:
            lines.append(_line(f"During {name}, peer healers cast {grp} {more} than you ({t['ch_share']:.0%} vs "
                               f"{y['ch_share']:.0%}), but the healing numbers don't show it pays off - no clear change."))
    return lines


def items_verdict(items: list[dict], spec: Spec) -> dict:
    if not items:
        return {}
    lines = []
    for i in items:
        name, kind = i["name"], i["kind"].lower()
        if not i["top_users"]:
            lines.append(_line(f"{name}: none of the comparable peer healers use it, so there's nothing to compare against."))
            continue
        users = f"{i['top_users']} of {i['top_count']} peer healers use it"
        if i["top_uses"] and i["uses"] < i["top_uses"] * ITEM_RATE_OK and (i["ready_pressure_s"] or 0) >= ITEM_READY_S:
            lines.append(_line(f"Press {name} more - {i['uses']} uses vs ~{i['top_uses']:.0f} for peer healers, and it "
                               f"sat ready through {i['ready_pressure_s']}s of pressure ({users}).", kind == "trinket",
                               i.get("ready_examples")))
        else:
            lines.append(_line(f"{name}: {i['uses']} uses, about as often as peer healers (~{i['top_uses']:.0f}; {users})."))
        if (kind == "trinket" and i["top_paired"] is not None and i["paired"] is not None and i["uses"] >= 3
                and i["top_paired"] >= 0.5 and i["paired"] <= i["top_paired"] - 0.3):
            lines.append(_line(f"Line {name} up with {spec.major_pair_text} - peer healers do {i['top_paired']:.0%} "
                               f"of the time, you {i['paired']:.0%}.", True))
    return _section(lines, "Trinkets & consumables: used about as often as peer healers - no change needed.",
                    "trinket use")


def phases_verdict(phases: list[dict]) -> dict:
    if not phases:
        return {}
    lines = []
    for p in phases:
        part = "start" if p["phase"] == "Start" else f"after {p['phase']}"  # parts are named after what began them
        where = f"{p['pull'].split(': ', 1)[-1]} ({part})"
        if p["top_group_hp"] is None or p["top_n"] < 2:
            continue
        gap = p["top_group_hp"] - p["group_hp"]
        if gap < PHASE_GAP:
            lines.append(_line(f"{where}: group HP as good as peer groups ({p['group_hp']:.0%} vs {p['top_group_hp']:.0%})."))
            continue
        cause = ("your group took more damage than peer groups" if p["top_intake"] and p["intake"] >= p["top_intake"] * 1.2
                 else "damage taken was similar, so healing throughput or cooldown timing")
        lines.append(_line(f"{where} - group sat at {p['group_hp']:.0%} vs {p['top_group_hp']:.0%} for peer groups; "
                           f"{cause}.", True, [f"Starts at {p['clock']}"]))
    return _section(lines, "Boss phases: group HP matched peer groups in every phase.", "boss phases")


def pulls_verdict(requirements: list[dict], phases: dict | None = None) -> dict:
    """Per pull. Boss fights with parts (Melidrussa before / after Awaken Whelps) are judged part by part, and those
    lines lead; the whole-boss line is then skipped."""
    if not requirements:
        return {}
    part_lines = [l for l in (phases or {}).get("lines", []) if l["action"]]
    covered = {l["text"].split(" (")[0] for l in part_lines}
    lines = list(part_lines)
    for r in requirements:
        if r["top_group_hp"] is None:
            continue
        name = r["pull"].split(": ", 1)[-1]
        if name.replace(" (boss)", "") in covered:
            continue
        low = r["group_hp"] < r["top_group_hp"] - PULL_HP_GAP
        heavy = r["top_intake"] and r["intake"] >= r["top_intake"] * PULL_INTAKE_RATIO
        if low and heavy:
            lines.append(_line(f"{name} - group lower than peer groups ({r['group_hp']:.0%} vs {r['top_group_hp']:.0%}) "
                               f"and took {r['intake'] / r['top_intake'] - 1:.0%} more damage: mostly a group-damage issue.", True))
        elif low:
            lines.append(_line(f"{name} - group lower than peer groups ({r['group_hp']:.0%} vs {r['top_group_hp']:.0%}) "
                               f"with similar damage taken: healing throughput or cooldowns.", True))
        elif heavy:
            lines.append(_line(f"{name}: your group took {r['intake'] / r['top_intake'] - 1:.0%} more damage than top "
                               f"groups, and you still kept them as healthy - nice."))
    ok = ("Pulls: group HP at or above peer groups on every pull." if not covered else
          f"Other pulls: group HP at or above peer groups ({', '.join(sorted(covered))} judged part by part).")
    return _section(lines, ok, "pulls")
