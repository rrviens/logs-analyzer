"""Context-aware findings. Each rule only fires when the moment mattered, and every
finding carries the evidence behind it."""
import statistics
from dataclasses import dataclass, field

from app.context import (CALM, EMERGENCY, GROUP_LOW_COUNT, IDLE, LETHAL_EHP, PRESSURE, Context,
                         CooldownTracker, low_count)
from app.context_model import DungeonModel
from app.deaths import ENVIRONMENTAL
from app.metrics import hots_active
from app.runs import RunData
from app.spellfacts import FACTS
from app.specs import RACIALS
from app.specs.base import Spec
from app.segments import MELEE_ID, RECOVERY_CAP_S, Profile, SegmentMetrics
from app.healer_state import HealerState
from app.spells import HealCast, buff_table, choice_table
from app.timeline import Timeline

LOW_VALUE_LOOKAHEAD_S = 10
ROTATIONAL_SHARE = 0.75  # top players' casts reach this share of the possible casts: a rotational cooldown
LATE_CD_S = 10
NOT_TOPPED_MARGIN = 0.10
INTAKE_RATIO = 1.25
INTAKE_MERGE = 2  # more damage-taken findings than this merge into one whole-run finding
RIPTIDE_GAP = 0.15
RIPTIDE_WASTE_S = 15
MIN_PRESSURE_BINS = 20
MIN_PULL_S = 10  # shorter "pulls" are log artifacts
PLAN_TOLERANCE_S = 15
PLAN_LOOKAHEAD_S = 15
RATE_RATIO = 0.7  # pressing a cooldown less than this share of top players' rate
RATE_MIN_READY_PRESSURE_S = 60
RIPTIDE_PREP_GAP = 2
EXPECTED_DEATHS = 1.0  # top groups average this many deaths on the pull
SUSTAINED_S = 3  # group danger must last this long before a group cooldown "should" have answered it
INTAKE_SANE = (0.5, 2.5)  # outside this ratio the matched top pull is a different pull size
SELF_DANGER_EHP, SELF_DANGER_S = 0.30, 3
MERGE_S = 3
DEATH_GROUP_MS = 1500  # deaths to the same ability this close are one event
WIPE_DEATHS, WIPE_WINDOW_S = 3, 45
LATE_PRESS_MS = 3000  # a cooldown pressed this close before a death came too late
READY_BEFORE_MS = 8000  # "ready and unused" = ready this long before the death
MIN_HIT_SAMPLES = 3  # top-group hit references need this many hits
REZ_GRACE_MS = 15000
END_OF_RUN_MS = 10000
RECOVERY_INTAKE_MAX = 1.2  # x the pull's normal damage intake
SLOW_RECOVERY_EXTRA_S, SLOW_RECOVERY_RATIO = 5, 2.0  # a few seconds slower is noise at high keys
WIPE_PRESS_SLACK_MS = 5000
HELD_MIN_EHP, HELD_LONG_S = 0.30, 5  # a held cooldown matters if someone got this low, or it lasted this long
PULL_IDLE_S = 30  # per-pull breakdown: only pulls where it sat ready through this much pressure
PAIR_WINDOW_MS = 10000  # trinket "lined up" with a major cooldown
ITEM_RATE_RATIO, ITEM_MIN_READY_PRESSURE_S = 0.7, 60
MIN_ITEM_CD_S = 30
MIN_SAME_COMP = 2  # top groups needed with your comp effects before comparing only against them
SPELL_STRETCH_MIN, SPELL_STRETCH_GAP_MS = 3, 6000  # "a run" of the same heal: 3+ casts, <=6s apart
CH_SINGLE_OVERHEAL = 0.4  # Chain Heals in a single-target stretch must have overhealed this much
CH_SINGLE_TOP_MAX = 0.4  # ...and top players must mostly not Chain Heal with one injured
HW_GROUP_TOP_MIN = 0.5  # Healing Wave stretches only flagged if top players mostly Chain Heal 3+ injured
SPELL_MIN_CASTS = 30  # enough heals in a situation to judge the habit
SPELL_SHARE_GAP = 0.15  # Chain Heal share this far below top players
SPELL_CH_GAIN = 0.2  # ...and your own Chain Heals healed this much more per cast there
MIN_PHASE_TOPS, PHASE_HP_GAP, PHASE_DAMAGE_RATIO = 2, 0.08, 1.2
LOW_MANA, LOW_MANA_S = 0.15, 15
BUFF_MIN_CASTS, BUFF_SHARE_GAP = 15, 0.2
ITEM_EXAMPLE_S = 15  # a trinket "sat ready" example must last this long
OVERLAP_WINDOW_MS = 5000  # big tools this close to one spike count as stacked on it
OVERLAP_AFTER_S = 6  # how long after the spike the group's HP is judged
OVERLAP_SAFE_GROUP, OVERLAP_SAFE_LOWEST = 0.6, 0.4  # stayed above these = one tool would have done
OVERLAP_SAFE_MIN = 3  # harmless overlaps are mentioned (as one whole-run line) from this many


@dataclass
class Finding:
    t: int  # ms, report time
    pull: str
    severity: int
    title: str
    detail: list[str] = field(default_factory=list)
    context: list[list[str]] = field(default_factory=list)  # [label, text]: Ready? / Needed? / Top runs
    rule: str = ""  # which rule produced it
    short: str = ""  # one line, ~10 words - the collapsed view
    fix: str = ""  # a few words - what to do


@dataclass
class TopRun:
    """A comparable top log, as the rules need it."""
    run: RunData
    trinkets: list[str]
    intake_modifiers: frozenset
    heal_casts: list[HealCast] = field(default_factory=list)


@dataclass
class Analysis:
    findings: list[Finding]
    good_calls: list[Finding]
    requirements: list[dict]
    items: list[dict] = field(default_factory=list)  # trinkets, potions, racials vs top players
    spell_choice: dict = field(default_factory=dict)  # single vs group heal by situation, you vs top


class _Ctx:
    """Shared lookups for the rules."""

    def __init__(self, run, tl, segs, profiles, ctx, model: DungeonModel | None,
                 tops: list[TopRun], my_trinkets: list[str], my_modifiers: frozenset, spec: Spec):
        self.run, self.tl, self.segs, self.profiles, self.ctx = run, tl, segs, profiles, ctx
        self.spec = spec
        self.top_label = f"Top {spec.plural}"  # the season study (top-ranked runs, usually higher keys)
        self.peer_label = f"Peer {spec.plural}"  # the comparable logs at your key level
        self.cds = CooldownTracker(run, spec.healing_cooldowns)
        self.model = model
        self.seg_models = [model.segment(s.pull) if model else None for s in segs]
        self.hots = hots_active(run, tl)
        self.covered: dict[int, list[tuple[int, int]]] = {}  # cooldown -> bin ranges already reported
        self.tops, self.my_trinkets, self.my_modifiers = tops, my_trinkets, my_modifiers
        self.minutes = (run.end - run.start) / 60000

    # --- the three context checks shown with cooldown findings ---

    def ready_text(self, aid: int, t: int, tracker: CooldownTracker | None = None) -> str:
        tr = tracker or self.cds
        casts = tr.casts.get(aid, [])
        cd = tr.cd_ms.get(aid, tr.cooldowns[aid][1] * 1000)
        last = max((x for x in casts if x <= t), default=None)
        if last is None:
            return "Yes - not pressed yet this run, ready since the start."
        if t - last >= cd:
            ready_at = last + cd
            return f"Yes - ready since {self.clock(ready_at)} ({_dur(t - ready_at)} unused)."
        return f"No - on cooldown until {self.clock(last + cd)} (pressed at {self.clock(last)})."

    def need_text(self, b0: int, b1: int) -> str:
        bins = range(max(0, b0), min(len(self.tl.group_ehp), b1 + 1))
        if not bins:
            return "-"
        deaths = [d for d in self.run.deaths if self.t_of(bins.start) <= d[0] <= self.t_of(bins.stop)]
        sustained = sum(1 for b in bins if low_count(self.tl, b) >= GROUP_LOW_COUNT)
        low = min(bins, key=lambda b: self.tl.min_ehp[b])
        who = f"{self.player(self.tl.lowest_player[low])} at {self.tl.min_ehp[low]:.0%}"
        if deaths:
            return f"Yes - {len(deaths)} death(s); 2+ players under 50% for {sustained}s, lowest {who}."
        if sustained >= SUSTAINED_S:
            return f"Yes - 2+ players under 50% for {sustained}s, lowest {who}."
        if self.tl.min_ehp[low] < HELD_MIN_EHP:
            return f"Partly - one player dropped ({who}); the rest of the group was fine."
        return f"No - lowest was {who}; group never below {min(self.tl.group_ehp[b] for b in bins):.0%}."

    def top_text(self, name: str, pull: int | None) -> str:
        sm = self.seg_models[pull] if pull is not None else None
        if not sm:
            return "No top-run data for this pull."
        use = sm["cooldowns"].get(name)
        runs = sm["runs"]
        if not use:
            return f"{self.top_label} don't press it on this pull (0 of {runs} runs)."
        when = (", ".join(f"~{p['at_s']}s in" for p in use["planned"]) if use["planned"]
                else f"usually ~{use['median_time_s']}s in")
        how = "mostly before the damage" if use["proactive_share"] >= 0.5 else "mostly as damage lands"
        return (f"Pressed on this pull in {round(use['share_of_runs'] * runs)} of {runs} top runs, "
                f"{when}, {how}.")

    def planned_near(self, aid: int, t: int) -> bool:
        """True if top players have a planned use of this cooldown near t on this boss."""
        pull = self.ctx.pull_of_bin[self.tl.bin_of(t)]
        sm = self.seg_models[pull] if pull is not None else None
        use = sm["cooldowns"].get(self.cds.name(aid)) if sm else None
        if not use:
            return False
        rel = (t - self.run.pulls[pull].start) / 1000
        return any(abs(rel - p["at_s"]) <= PLAN_TOLERANCE_S for p in use["planned"])

    def t_of(self, b: int) -> int:
        return self.tl.start + b * self.tl.bin_ms

    def clock(self, t: int) -> str:
        s = max(0, (t - self.run.start) // 1000)
        return f"{s // 60}:{s % 60:02d}"

    def pull_label(self, idx: int | None) -> str:
        if idx is None:
            return "between pulls"
        p = self.run.pulls[idx]
        return f"Pull {idx + 1}: {p.name}" + (" (boss)" if p.is_boss else "")

    def ability(self, aid: int) -> str:
        return self.run.ability_name(aid)

    def player(self, pid: int) -> str:
        return self.run.player_name(pid)


def analyze(run: RunData, tl: Timeline, segs: list[SegmentMetrics], profiles: list[Profile | None],
            ctx: Context, model: DungeonModel | None = None, tops: list[TopRun] | None = None,
            my_trinkets: list[str] | None = None, my_modifiers: frozenset = frozenset(),
            heal_casts: list[HealCast] | None = None, phases: dict | None = None,
            phase_rows: list[dict] | None = None, state: HealerState | None = None,
            death_recaps: list[dict] | None = None, *, spec: Spec,
            group_defs: list[tuple] | None = None, went_through: list[dict] | None = None) -> Analysis:
    """group_defs: other players' group defensive presses [(t, name, who)] - for healing overlaps."""
    profiles = [None if s.duration_s < MIN_PULL_S else p for s, p in zip(segs, profiles)]
    c = _Ctx(run, tl, segs, profiles, ctx, model, tops or [], my_trinkets or [], my_modifiers, spec)
    c.heal_casts = heal_casts or []
    c.went_through = went_through or []  # enemy casts peers usually stop that completed here
    c.recaps = {(r["t"], r["victim"]): r for r in death_recaps or []}
    c.phases, c.phase_rows, c.state = phases or {}, phase_rows or [], state
    c.group_defs = group_defs or []

    def tag(found: list[Finding], rule: str) -> list[Finding]:
        for f in found:
            f.rule = f.rule or rule
        return found

    findings: list[Finding] = []
    findings += tag(_deaths(c), "death")
    findings += tag(_planned_cooldowns(c), "planned_cooldown")
    findings += tag(_held_cooldowns(c), "held_cooldown")
    findings += tag(_cooldown_rate(c), "cooldown_rate")
    findings += tag(_riptide_prep(c), "riptide_prep")
    findings += tag(_self_danger(c), "self_danger")
    low_value, good = _cooldown_uses(c)
    findings += tag(low_value, "low_value_cooldown")
    findings += tag(_overlaps(c), "overlap")
    findings += tag(_not_topped(c), "not_topped")
    findings += tag(_slow_recovery(c), "slow_recovery")
    findings += tag(_damage_intake(c), "damage_intake")
    findings += tag(_riptide_under_pressure(c), "riptide_pressure")
    items, item_findings = _items(c)
    findings += tag(item_findings, "items")
    choice, choice_findings = _spell_choice(c)
    findings += tag(choice_findings, "spell_choice")
    findings += tag(_phase_gaps(c), "phase_gap")
    findings += tag(_low_mana(c), "low_mana")
    findings = _merge_same_moment(findings)
    for f in findings + good:
        f.pull += _phase_suffix(c, f.t)
    for f in findings:
        f.short, f.fix = _shorthand(f, spec)
    findings.sort(key=lambda f: -f.severity)
    return Analysis(findings, sorted(good, key=lambda f: f.t), _requirements(c), items, choice)


def _fixes(spec: Spec) -> dict[str, str]:
    """Rule -> short fix. Rule keys keep their original (Resto Shaman) names: riptide_* = maintenance HoT."""
    hot = spec.maintenance.plural if spec.maintenance else "HoTs"
    personal = next(iter(spec.personal_cooldowns.values()), ("a personal", 0))[0]
    group = spec.spell_pair.group if spec.spell_pair else "group heals"
    return {
        "held_cooldown": "Press it into the damage", "planned_cooldown": "Press it on the timing",
        "cooldown_rate": "Press it on cooldown", "riptide_prep": f"3 {hot} out first",
        "not_topped": "Top them before the hit", "self_danger": personal, "low_value_cooldown": "Save it for damage",
        "slow_recovery": "Faster heals after it", "damage_intake": "Group: avoidable damage",
        "riptide_pressure": f"Keep {hot} rolling", "spell_choice": f"More {group}", "buff_choice": f"More {group}",
        "low_mana": "Manage mana earlier", "overlap": "One cooldown per spike",
    }


def _shorthand(f: Finding, spec: Spec) -> tuple[str, str]:
    """Collapsed view: a ~10-word line and a short fix."""
    title = f.title
    for long, short in spec.short_names.items():
        title = title.replace(long, short)
    fix = f.fix or _fixes(spec).get(f.rule, "")
    if f.rule == "death":
        title = title.replace(" died - mostly ", " died in ").split(" (")[0]
    elif f.rule == "held_cooldown":
        title = title.split(" held through")[0] + " held through danger"
    elif f.rule == "cooldown_rate":
        title = title.replace(" pressed far less than top players", " pressed less than top players")
    elif f.rule == "items":
        fix = (f"Pair with {spec.major_pair_text.replace(' or ', ' / ')}" if "not lined up" in title
               else "Press it on cooldown")
    elif f.rule == "phase_gap":
        title = title.replace("Group lower than peer groups ", "Low group HP ")
        fix = "Group: less damage taken" if any("more damage" in d for d in f.detail) else "More healing / cooldowns"
    words = title.split()
    return (" ".join(words[:12]) + ("..." if len(words) > 12 else "")), fix


def _phase_suffix(c: _Ctx, t: int) -> str:
    pull = c.ctx.pull_of_bin[c.tl.bin_of(t)]
    for ph in c.phases.get(pull, []):
        if ph.start <= t < ph.end:
            return "" if ph.name == "Start" else f" · after {ph.name}"
    return ""


def _phase_gaps(c: _Ctx) -> list[Finding]:
    """A boss phase where your group sat clearly lower than top groups in the same phase -
    and whether that came from extra damage or from healing/cooldowns."""
    out = []
    for row in c.phase_rows:
        if row["top_n"] < MIN_PHASE_TOPS or row["top_group_hp"] is None:
            continue
        gap = row["top_group_hp"] - row["group_hp"]
        if gap < PHASE_HP_GAP:
            continue
        damage_ratio = row["intake"] / row["top_intake"] if row["top_intake"] else None
        part = "the start of the fight" if row["phase"] == "Start" else f"the part after {row['phase']}"
        detail = [f"Group HP through {part}: {row['group_hp']:.0%} vs {row['top_group_hp']:.0%} in peer groups "
                  f"({row['top_n']} runs)."]
        if damage_ratio and damage_ratio >= PHASE_DAMAGE_RATIO:
            detail.append(f"Your group took {damage_ratio - 1:.0%} more damage here - partly avoidable or unmitigated damage.")
        elif damage_ratio:
            detail.append("Damage taken was similar to peer groups - the gap is healing throughput or cooldown timing.")
        for name, cd in row["cooldowns"].items():
            detail.append(f"{name}: peer healers pressed it in this part in {cd['top']} of {row['top_n']} runs; "
                          f"you {'did' if cd['you'] else 'did not'}.")
        if row["deaths"]:
            detail.append(f"{row['deaths']} death(s) in this part.")
        pull = c.pull_label(row["pull_idx"])
        f = Finding(row["start"], pull, 40 + min(20, int(gap * 100)) + (10 if row["deaths"] else 0),
                    f"Group lower than peer groups {'at the start' if row['phase'] == 'Start' else 'after ' + row['phase']}",
                    detail)
        out.append(f)
    return out


def _low_mana(c: _Ctx) -> list[Finding]:
    """Stretches under LOW_MANA while the group was in danger."""
    if not c.state or not c.state.mana:
        return []
    out, b, n = [], 0, len(c.tl.group_ehp)
    while b < n:
        m = c.state.mana_at(c.t_of(b))
        if m is None or m >= LOW_MANA or c.ctx.pull_of_bin[b] is None:
            b += 1
            continue
        start = b
        while b < n and (c.state.mana_at(c.t_of(b)) or 1) < LOW_MANA and c.ctx.pull_of_bin[b] is not None:
            b += 1
        dangers = [w for w in c.ctx.windows if start <= w.start < b and (w.deaths or w.sustained_group_s >= SUSTAINED_S)]
        if b - start < LOW_MANA_S or not dangers:
            continue
        lowest = min(c.state.mana_at(c.t_of(k)) or 1 for k in range(start, b))
        out.append(Finding(c.t_of(start), c.pull_label(c.ctx.pull_of_bin[start]), 40 + (10 if any(w.deaths for w in dangers) else 0),
                           f"Under {LOW_MANA:.0%} mana for {b - start}s during dangerous moments", [
            f"Mana bottomed out at {lowest:.0%}; {len(dangers)} group-wide dangerous moment(s) in that stretch.",
            f"Low mana limits {c.spec.mana_spells or 'your bigger heals'} exactly when they're needed."]))
    return out


def _merge_same_moment(findings: list[Finding]) -> list[Finding]:
    """Several rules can describe one moment (e.g. you were low AND a group cooldown was
    held). Show it once, led by the most severe angle."""
    merged: list[Finding] = []
    for f in sorted(findings, key=lambda f: -f.severity):
        host = next((m for m in merged if m.pull == f.pull and abs(m.t - f.t) <= MERGE_S * 1000), None)
        if host:
            host.detail.append(f"Also: {f.title}.")
            host.detail.extend(f"  {d}" for d in f.detail)
            host.context = host.context or f.context
        else:
            merged.append(Finding(f.t, f.pull, f.severity, f.title, list(f.detail), list(f.context), f.rule, fix=f.fix))
    return merged


def _deaths(c: _Ctx) -> list[Finding]:
    """One finding per death (deaths to the same hit together). When several deaths
    follow on one pull, only what started the wipe is analyzed; the rest are listed."""
    groups: list[list[list]] = []
    for d in sorted(c.run.deaths, key=lambda d: d[0]):
        if not d[2] and d[0] >= c.run.end - END_OF_RUN_MS:
            continue  # unattributed deaths as the key ends are resets, not mechanics
        if groups and d[2] == groups[-1][0][2] and d[0] - groups[-1][-1][0] <= DEATH_GROUP_MS:
            groups[-1].append(d)
        else:
            groups.append([d])

    # Deaths after the tank went down in a wipe: listed on the tank's death, no finding of their own.
    after_tank = {(a["clock"], a["victim"]) for r in c.recaps.values() for a in r.get("after_tank", [])}
    out, i = [], 0
    while i < len(groups):
        first = groups[i]
        if all((c.clock(d[0]), c.player(d[1])) in after_tank for d in first):
            i += 1
            continue
        pull = c.ctx.pull_of_bin[c.tl.bin_of(first[0][0])]
        j = i + 1
        while (j < len(groups) and c.ctx.pull_of_bin[c.tl.bin_of(groups[j][0][0])] == pull
               and groups[j][0][0] - first[0][0] <= WIPE_WINDOW_S * 1000):
            j += 1
        cluster = groups[i:j]
        f = _death_finding(c, first)
        total = sum(len(g) for g in cluster)
        if total >= WIPE_DEATHS:
            span = (cluster[-1][-1][0] - first[0][0]) / 1000
            f.title = "Wipe started: " + f.title
            f.detail.insert(0, f"{total} deaths in {span:.0f}s on this pull - this is what started it.")
            f.detail.append("Then: " + "; ".join(
                f"{c.clock(g[0][0])} {_names(c, g)} ({c.ability(g[0][2])})" for g in cluster[1:]) + ".")
            lines, delayed = _wipe_cooldowns(c, first[0][0], cluster[-1][-1][0])
            f.detail += lines
            f.severity = min(100, f.severity + 10 + (10 if delayed else 0))
            i = j
        else:
            i += 1
        out.append(f)
    return out


def _wipe_cooldowns(c: _Ctx, t0: int, t_end: int) -> tuple[list[str], bool]:
    """What each group cooldown was doing while the wipe unfolded."""
    lines, delayed = [], False
    for a in c.cds.casts:
        name, last = c.cds.name(a), c.cds.last_before(a, t0)
        back = c.cds.free_at(a, t0)
        if back > t_end:
            lines.append(f"{name}: on cooldown through all of it (used at {c.clock(last)}).")
            continue
        nxt = c.cds.next_after(a, back - 1)
        if nxt is None or nxt > t_end:
            delayed = True
            lines.append(f"{name}: available from {c.clock(back)} and not pressed before the last death.")
        elif nxt - back > WIPE_PRESS_SLACK_MS:
            delayed = True
            lines.append(f"{name}: came back at {c.clock(back)} but was pressed at {c.clock(nxt)} "
                         f"({(nxt - back) / 1000:.0f}s later) while people were dying.")
        else:
            lines.append(f"{name}: pressed as soon as it was available ({c.clock(nxt)}).")
    return lines, delayed


def _death_finding(c: _Ctx, group: list[list]) -> Finding:
    recap = c.recaps.get((group[0][0], c.player(group[0][1])))
    if recap:
        return _death_from_recap(c, group, recap)
    return _death_basic(c, group)


def _death_from_recap(c: _Ctx, group: list[list], r: dict) -> Finding:
    """Death finding built from the recap: real cause, the debuff/dispel story with advice,
    what was ready (or why not), and what the player was doing in their last seconds."""
    t, victim, killing = group[0]
    b = c.tl.bin_of(t)
    pull = c.ctx.pull_of_bin[b]
    cause = r["cause"]
    killing_name = c.ability(killing)
    title = (f"{_names(c, group)} died to {cause['name']}" if cause["name"] == killing_name
             else f"{_names(c, group)} died - mostly {cause['name']} ({cause['share']:.0%} of the damage)")
    detail = list(cause["lines"][:2])
    if cause["name"] != killing_name:
        detail.append(f"Killing blow: {killing_name} - it only finished them off.")

    series = c.tl.player_ehp[victim]
    before = series[max(0, b - 3)]
    low_secs = 0
    for k in range(b - 1, max(-1, b - 16), -1):
        if series[k] >= 0.5:
            break
        low_secs += 1
    burst = before >= 0.7 and low_secs < 2
    if burst:
        detail.append(f"Went from {before:.0%} to dead in under 3s - too fast to heal reactively; a defensive pressed "
                      f"before it lands is the answer.")
    elif low_secs >= 3:
        detail.append(f"Below 50% for {low_secs}s before dying.")

    advice = []
    if r.get("story"):
        detail += r["story"]["lines"]
        advice = r["story"]["advice"]

    # Their own casts while low - e.g. healing someone else at 33%.
    on_others = [x for x in r.get("last_casts", []) if x["target"] not in ("self", "") and x["hp"] is not None
                 and x["hp"] < 0.5]
    if on_others:
        detail.append("While low they cast on others: " + "; ".join(
            f"{x['ability']} on {x['target']} at {x['hp']:.0%} HP ({x['dt']:.1f}s)" for x in on_others[:3]) + ".")
    casts = [x for x in r.get("last_casts", []) if x["target"]][-5:]
    if casts:
        whose = "Your" if victim == c.run.healer_id else f"{c.player(victim)}'s"
        detail.append(f"{whose} last casts: " + "; ".join(
            f"{x['ability']} on {'yourself' if x['target'] == 'self' and victim == c.run.healer_id else x['target']}"
            + (f" at {x['hp']:.0%} HP" if x["hp"] is not None else "") + f" ({x['dt']:.1f}s)" for x in casts) + ".")

    # A boss or mob ability is the victim's to survive: others' externals and group cooldowns are context, never
    # "would have saved them" or the fix.
    others = [i for k in ("healer", "group") for i in r["ready"][k]
              if not i.get("escape") and i["status"] == "ready" and i.get("verdict") == "lived"]
    every = [i for i in r["ready"]["victim"] if not i.get("escape")]
    # Personals with no time to react are context only: never a miss, never weight.
    no_window = [i for i in every if i.get("no_window") and i["status"] == "ready"]
    every = [i for i in every if not i.get("no_window")]
    dodge, pull_melee, skip = r.get("dodge"), r.get("pull_melee"), r.get("skip")
    if dodge or pull_melee or skip:  # dodge it / the DPS's own threat: no defensive is the answer, so none counts as missed
        every, no_window = [], []
    savers = [i for i in every if i["status"] == "ready" and i.get("verdict") == "lived"]
    hard_savers = [i for i in savers if not i.get("gcd")]  # GCD defensives: holding them is often right
    ready_any = [i for i in every if i["status"] == "ready"]
    if skip:
        ready_line = (f"Not a healing problem: a skip went wrong - {_names(c, group)} died alone, nobody else was in "
                      f"combat, and the group was skipping ({skip}).")
    elif pull_melee:
        ready_line = (f"Not a healing problem: {_names(c, group)} took mob melee {pull_melee}. A DPS the mobs go for "
                      f"first is one-shot before any healer can react - let the tank get threat, stay out of melee "
                      f"range of the pack" + (". Only a Blessing of Protection pressed before the hit would save it."
                                              if c.spec.class_name == "Paladin" else "."))
    elif dodge:
        ready_line = (f"Not a defensive problem: {dodge['ability']} is damage to dodge ({dodge['why']}). "
                      f"Standing in it was the mistake.")
    elif savers:
        ready_line = "\n".join(
            f"{i['name']} ({i['owner']}) - would likely have saved them, ~{i['lowest']:.0%}"
            + (" (costs a GCD - holding it can be right)" if i.get("gcd") else "") for i in savers) + "."
    elif ready_any:
        ready_line = "\n".join(f"{i['name']} ({i['owner']}) - ready, but not enough on its own." for i in ready_any)
    else:
        why = [f"{i['name']} ({i['owner']}): {i['text']}" for i in every if i["status"] == "cooldown"][:4]
        ready_line = "\n".join(["Nothing - these were on cooldown:", *why] if why else ["Nothing - no tracked defensives for this group."])

    sm = c.seg_models[pull] if pull is not None else None
    expected = sm is not None and sm["deaths_per_run"] >= EXPECTED_DEATHS
    if expected:
        detail.append(f"Even top players' groups average {sm['deaths_per_run']:g} deaths on this pull.")
    saver_weight = 30 if hard_savers else 10 if savers else 0
    severity = (50 + (saver_weight if not burst else 0) + (15 if low_secs >= 3 else 0) + (10 if advice else 0)
                - (20 if burst and not savers else 0) - (15 if expected else 0))
    if pull_melee or skip:
        severity = min(severity, 30)  # their threat / positioning, not your healing
    context = [["Ready?", ready_line]]
    if no_window:
        context.append(["No reaction window", f"{' and '.join(i['name'] for i in no_window)} - "
                        f"{'was' if len(no_window) == 1 else 'were'} ready, but there was no time to react "
                        f"({r.get('reaction', {}).get('why', '')}); not a mistake."])
    if others and not dodge and not pull_melee and not skip:
        context.append(["Group tools", "\n".join(f"{i['name']} ({i['owner']}) - up, but not expected to be spent on this."
                                                 for i in others)])
    if advice:
        context.append(["What to do", " ".join(advice)])
    fix = ("Skip went wrong - not healing" if skip else "Let the tank get threat first" if pull_melee else f"Dodge {dodge['ability']}" if dodge else
           (r["story"] or {}).get("fix") or (f"Use {hard_savers[0]['name']}" if hard_savers
                                             and r["cause"]["name"] not in ENVIRONMENTAL else ""))
    return Finding(t, c.pull_label(pull), min(100, severity), title, detail, context, fix=fix)


def _death_basic(c: _Ctx, group: list[list]) -> Finding:
    t, victim, killing = group[0]
    victims = [d[1] for d in group]
    b = c.tl.bin_of(t)
    pull = c.ctx.pull_of_bin[b]
    series = c.tl.player_ehp[victim]
    before = series[max(0, b - 3)]
    low_secs = 0
    for k in range(b - 1, max(-1, b - 16), -1):
        if series[k] >= 0.5:
            break
        low_secs += 1
    burst = before >= 0.7 and low_secs < 2

    detail = []
    if burst:
        detail.append(f"Went from {before:.0%} to dead in under 3s - too fast to heal reactively. A defensive "
                      f"pressed before it lands is the answer - see the death recap for what was ready.")
    elif low_secs >= 3:
        detail.append(f"Was below 50% for {low_secs}s before dying.")

    if pull is not None:
        hit = next((h for h in reversed(c.segs[pull].hits) if t - 3000 <= h.t <= t), None)
        if hit and (not burst or hit.ability == killing):
            prof = c.profiles[pull]
            top = prof.hit_pre.get(hit.ability) if prof else None
            line = f"Group was at {hit.pre_ehp:.0%} before {c.ability(hit.ability)}"
            detail.append(line + (f"; peer groups hold {top:.0%} before it." if top else "."))

    # A healer dying alone is a personal-defensive question, not a group-cooldown one.
    if victims == [c.run.healer_id]:
        pool, candidates = CooldownTracker(c.run, c.spec.personal_cooldowns), list(c.spec.personal_cooldowns)
    else:
        pool, candidates = c.cds, list(c.cds.casts)
    ready, late = [], []
    for a in candidates:
        if a not in pool.casts:
            ready.append(pool.name(a))
            continue
        recent = [x for x in pool.casts[a] if t - LATE_PRESS_MS <= x <= t + 500]
        if recent:
            late.append(f"{pool.name(a)} was pressed only {max(0, t - recent[0]) / 1000:.0f}s before the death.")
        elif pool.ready(a, t - READY_BEFORE_MS) and not pool.used_between(a, t - READY_BEFORE_MS, t):
            ready.append(pool.name(a))
    if ready and not burst:
        detail.append(", ".join(ready) + " - ready and unused.")
    detail += late
    if c.ctx.defensive[b]:
        detail.append("Team defensives were active at the time.")
    sm = c.seg_models[pull] if pull is not None else None
    expected = sm is not None and sm["deaths_per_run"] >= EXPECTED_DEATHS
    if expected:
        detail.append(f"Even top players' groups average {sm['deaths_per_run']:g} deaths on this pull.")

    severity = (50 + (30 if ready and not burst else 0) + (15 if low_secs >= 3 else 0)
                - (20 if burst else 0) - (15 if expected else 0))
    return Finding(t, c.pull_label(pull), severity, f"{_names(c, group)} died to {c.ability(killing)}", detail)


def _names(c: _Ctx, group: list[list]) -> str:
    names = [c.player(d[1]) for d in group]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _held_cooldowns(c: _Ctx) -> list[Finding]:
    """Group-wide danger while a healing cooldown sat ready. Tank-only dips and deaths
    (covered by _deaths) are skipped. Moments held before the same next use are merged."""
    out = []
    held_by: dict[tuple[int, int | None], list] = {}
    for w in c.ctx.windows:
        if w.deaths or w.sustained_group_s < SUSTAINED_S:
            continue  # a dip healed back within a couple of GCDs was handled
        if w.min_ehp >= HELD_MIN_EHP and w.sustained_group_s < HELD_LONG_S:
            continue  # nobody got close to dying and it recovered on its own
        t0, t1 = c.t_of(w.start), c.t_of(w.end + 1)
        if any(c.cds.used_between(a, t0 - 3000, t1 + 3000) for a in c.cds.casts):
            continue  # another major cooldown answered this moment
        for a in c.cds.casts:
            if not c.cds.ready(a, t0):
                continue
            if c.cds.name(a) in c.spec.ramp_cooldowns and not c.planned_near(a, t0):
                continue  # a planned ramp: only top players' timings judge it (planned-cooldown and rate rules)
            if any(s <= w.end and w.start <= e for s, e in c.covered.get(a, [])):
                continue  # already reported as a missed planned use
            nxt = c.cds.next_after(a, t1)
            if nxt and nxt - t1 <= LATE_CD_S * 1000:
                out.append(Finding(t0, c.pull_label(w.pull_idx), 40 + (15 if w.min_ehp < LETHAL_EHP else 0),
                                   f"Late {c.cds.name(a)}", [
                    f"{c.cds.name(a)} came {(nxt - t1) / 1000:.0f}s after the danger had passed."],
                    context=[["Ready?", c.ready_text(a, t0)], ["Needed?", c.need_text(w.start, w.end)],
                             ["Top players", c.top_text(c.cds.name(a), w.pull_idx)]]))
            else:
                held_by.setdefault((a, nxt), []).append(w)

    for (a, nxt), windows in held_by.items():
        name = c.cds.name(a)
        uncovered = [w for w in windows if w.defensive_share < 0.5]
        worst = min(windows, key=lambda w: w.min_ehp)
        detail = [
            f"{c.clock(c.t_of(w.start))} {c.pull_label(w.pull_idx)}: {w.sustained_group_s}s with 2+ players "
            f"under 50%, lowest {c.player(w.lowest_player)} at {w.min_ehp:.0%}"
            + (" (team defensives up)" if w.defensive_share >= 0.5 else "")
            for w in windows[:4]
        ]
        if len(windows) > 4:
            detail.append(f"...and {len(windows) - 4} more.")
        if nxt:
            nb = c.tl.bin_of(nxt)
            line = f"Next used at {c.clock(nxt)} ({c.pull_label(c.ctx.pull_of_bin[nb])}"
            if c.ctx.labels[nb] in EMERGENCY:
                line += ", where it was needed). If that was the plan, the earlier moments were the cost"
            else:
                line += f", {c.ctx.labels[nb]} moment)"
            detail.append(line + ".")
        else:
            detail.append("Not used again this run.")
        if not uncovered:
            detail.append("Team defensives covered these - holding was reasonable.")
        sm = c.seg_models[windows[0].pull_idx]
        rarely = sm is not None and sm["cooldowns"].get(name, {}).get("share_of_runs", 0) < 0.3
        if rarely:
            detail.append(f"{c.top_label} rarely use it on this pull either.")

        severity = (40 + (20 if worst.min_ehp < LETHAL_EHP else 0) + 5 * min(len(uncovered), 4)
                    - (25 if not uncovered else 0) - (15 if rarely else 0))
        plural = "s" if len(windows) > 1 else ""
        first = windows[0]
        context = [["Ready?", c.ready_text(a, c.t_of(first.start))],
                   ["Needed?", c.need_text(first.start, first.end)],
                   ["Top players", c.top_text(name, first.pull_idx)]]
        out.append(Finding(c.t_of(first.start), c.pull_label(first.pull_idx), severity,
                           f"{name} held through {len(windows)} dangerous moment{plural}", detail, context))
    return out


def _planned_cooldowns(c: _Ctx) -> list[Finding]:
    """Boss timings where most top players press a healing cooldown, and you had it ready
    but didn't - only reported when danger followed."""
    out = []
    for i, seg in enumerate(c.segs):
        sm = c.seg_models[i]
        if not sm or not seg.pull.is_boss:
            continue
        for a in c.cds.casts:
            name = c.cds.name(a)
            use = sm["cooldowns"].get(name)
            for plan in (use or {}).get("planned", []):
                t = seg.pull.start + plan["at_s"] * 1000
                tol = PLAN_TOLERANCE_S * 1000
                if t > seg.pull.end or c.cds.used_between(a, t - tol, t + tol) or not c.cds.ready(a, t - tol):
                    continue
                after = range(c.tl.bin_of(t), c.tl.bin_of(min(seg.pull.end, t + PLAN_LOOKAHEAD_S * 1000)) + 1)
                deaths = [d for d in c.run.deaths if t <= d[0] <= t + PLAN_LOOKAHEAD_S * 1000]
                sustained = sum(1 for b in after if low_count(c.tl, b) >= GROUP_LOW_COUNT)
                if not deaths and sustained < SUSTAINED_S:
                    continue  # skipping it cost nothing this time
                low = min(after, key=lambda b: c.tl.min_ehp[b])
                nxt = c.cds.next_after(a, t)
                detail = [f"You pressed it at {c.clock(nxt)} instead ({(nxt - t) / 1000:.0f}s later)." if nxt
                          else "You didn't press it on this boss."]
                answers = [h for h in sm["group_hits"] if h["answered_by"].get(name, 0) >= 0.3]
                if answers:
                    detail.append(f"Top players use it to answer {answers[0]['name']} "
                                  f"({answers[0]['answered_by'][name]:.0%} of those hits).")
                c.covered.setdefault(a, []).append((after.start - PLAN_TOLERANCE_S, after.stop))
                severity = 55 + (20 if deaths else 10 if c.tl.min_ehp[low] < LETHAL_EHP else 0)
                context = [["Ready?", c.ready_text(a, t)],
                           ["Needed?", c.need_text(after.start, after.stop - 1) + f" (in the {PLAN_LOOKAHEAD_S}s after)"],
                           ["Top players", c.top_text(name, i)]]
                out.append(Finding(t, c.pull_label(i), severity, f"Missed planned {name}", detail, context))
    return out


def _cooldown_rate(c: _Ctx) -> list[Finding]:
    """Top players press some cooldowns nearly on cooldown. Only flagged when yours sat
    ready through a lot of pressure, so a quiet run never triggers it."""
    if not c.model:
        return []
    out = []
    minutes = (c.run.end - c.run.start) / 60000
    for aid, (name, _) in c.spec.healing_cooldowns.items():
        top_rate = c.model.casts_per_min.get(name, 0)
        if top_rate <= 0:
            continue
        casts = c.cds.casts.get(aid, [])
        if len(casts) >= top_rate * minutes * RATE_RATIO:
            continue
        cd_ms = c.cds.cd_ms.get(aid, c.spec.healing_cooldowns[aid][1] * 1000)

        def ready(t: int) -> bool:
            last = max((x for x in casts if x <= t), default=None)
            return last is None or t - last >= cd_ms

        ready_bins = [ready(c.t_of(b)) for b in range(len(c.ctx.labels))]
        ready_pressure = sum(1 for b, lab in enumerate(c.ctx.labels) if lab in (PRESSURE, *EMERGENCY) and ready_bins[b])
        if ready_pressure < RATE_MIN_READY_PRESSURE_S:
            continue
        danger_windows = [w for w in c.ctx.windows
                          if (w.deaths or w.sustained_group_s >= SUSTAINED_S) and ready(c.t_of(w.start))]

        # Per pull: where top players press it and you didn't, and how much pressure it sat through.
        detail = []
        for i, seg in enumerate(c.segs):
            if seg.duration_s < MIN_PULL_S:
                continue
            mine = sum(1 for x in casts if seg.pull.start <= x < seg.pull.end)
            sm = c.seg_models[i]
            use = sm["cooldowns"].get(name) if sm else None
            share = use["share_of_runs"] if use else 0.0
            idle = sum(1 for b in seg.bins if ready_bins[b] and c.ctx.labels[b] in (PRESSURE, *EMERGENCY))
            dangers = sum(1 for w in danger_windows if w.pull_idx == i)
            if mine or idle < PULL_IDLE_S:
                continue  # pressed here, or it barely mattered
            top = (f"top players press it here in {round(share * sm['runs'])} of {sm['runs']} runs"
                   + (f" (~{use['median_time_s']}s in)" if use else "")) if sm else "no top-run data for this pack"
            detail.append(f"{c.pull_label(i)}: not pressed - {top}; sat ready through {idle}s of pressure"
                          + (f", {dangers} dangerous moment(s)." if dangers else "."))
        if not detail:
            continue
        stretches = _ready_stretches(ready_bins, c)
        longest = max(stretches, key=lambda s: s[1] - s[0], default=None)
        context = [
            ["Ready?", f"Unused while ready for {_dur(sum(e - s for s, e in stretches) * 1000)} in total"
                       + (f"; longest stretch {c.clock(c.t_of(longest[0]))}-{c.clock(c.t_of(longest[1]))}."
                          if longest else ".")],
            ["Needed?", f"{ready_pressure / 60:.1f} min of pressure while it was ready, "
                        f"{len(danger_windows)} group-wide dangerous moment(s)."],
            ["Top players", f"{c.top_label} in this dungeon press it ~{top_rate * minutes:.0f} times in a run this "
                         f"long (about every {60 / top_rate:.0f}s); you pressed it {len(casts)} times."],
        ]
        out.append(Finding(c.run.start, "Whole run", 45 + (15 if len(danger_windows) >= 2 else 0),
                           f"{name} pressed far less than top players", detail[:6], context))
    return out


def _ready_stretches(ready_bins: list[bool], c: _Ctx) -> list[tuple[int, int]]:
    """Stretches (bins) inside pulls where a cooldown sat ready."""
    out, start = [], None
    for b, r in enumerate(ready_bins + [False]):
        live = r and b < len(c.ctx.pull_of_bin) and c.ctx.pull_of_bin[b] is not None
        if live and start is None:
            start = b
        elif not live and start is not None:
            out.append((start, b))
            start = None
    return out


def _dur(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _riptide_prep(c: _Ctx) -> list[Finding]:
    """Top players have ~3 maintenance HoTs (Riptides) out before big group hits. Flag hits you
    went into with far fewer that then left someone near death. ("riptides_*" model keys hold
    the spec's maintenance HoT.)"""
    out = []
    if not c.spec.maintenance:
        return out
    hot = c.spec.maintenance
    for i, seg in enumerate(c.segs):
        sm = c.seg_models[i]
        if not sm:
            continue
        model_hits = {h["ability"]: h for h in sm["group_hits"]}
        for ability, hits in _by_ability(seg).items():
            mh = model_hits.get(ability)
            if not mh or mh["riptides_median"] < RIPTIDE_PREP_GAP:
                continue
            short = [h for h in hits
                     if c.hots[max(0, c.tl.bin_of(h.t) - 1)] <= mh["riptides_median"] - RIPTIDE_PREP_GAP
                     and h.min_after < LETHAL_EHP]
            if len(short) < 2:
                continue
            avg = statistics.mean(c.hots[max(0, c.tl.bin_of(h.t) - 1)] for h in short)
            out.append(Finding(short[0].t, c.pull_label(i), min(55, 30 + 5 * len(short)),
                               f"{hot.name} prep before {c.ability(ability)}", [
                f"{len(short)} of {len(hits)} times you had {avg:.1f} {hot.plural} out before it "
                f"(top players: {mh['riptides_median']:g}), and it left someone below {LETHAL_EHP:.0%}."]))
    return out


def _cooldown_uses(c: _Ctx) -> tuple[list[Finding], list[Finding]]:
    low_value, good = [], []
    labels = c.ctx.labels
    for a, casts in c.cds.casts.items():
        name = c.cds.name(a)
        # A cooldown top players press nearly on cooldown (e.g. Holy Paladin's Avenging Wrath) is rotational:
        # pressing it into a calm moment is normal, not a waste.
        top_rates = [sum(1 for _, x, _ in tr.run.casts if x == a) / max(1.0, (tr.run.end - tr.run.start) / 60000)
                     for tr in c.tops]
        rotational = bool(top_rates) and statistics.median(top_rates) * c.cds.cd_ms[a] / 60000 >= ROTATIONAL_SHARE
        for t in casts:
            b = c.tl.bin_of(t)
            pull = c.ctx.pull_of_bin[b]
            around = range(max(0, b - 3), min(len(labels), b + 4))
            if any(labels[k] in EMERGENCY for k in around):
                low = min(around, key=lambda k: c.tl.min_ehp[k])
                lowest = c.tl.lowest_player[low]
                just_rezzed = any(d[1] == lowest and t - REZ_GRACE_MS <= d[0] <= t for d in c.run.deaths)
                if c.tl.min_ehp[low] < 0.3 and not just_rezzed:
                    why = f"saved {c.player(c.tl.lowest_player[low])} at {c.tl.min_ehp[low]:.0%}."
                else:
                    why = f"pressed into a damage spike (group down to {min(c.tl.group_ehp[k] for k in around):.0%})."
                if c.planned_near(a, t):
                    why += " Matches top players' timing on this boss."
                good.append(Finding(t, c.pull_label(pull), 0, f"{name} at {c.clock(t)}", [why[0].upper() + why[1:]]))
                continue
            ahead = range(b, min(len(labels), b + LOW_VALUE_LOOKAHEAD_S + 1))
            if rotational or not all(labels[k] in (CALM, IDLE) for k in ahead) or c.planned_near(a, t):
                continue
            if pull is not None and c.run.pulls[pull].end - t < LOW_VALUE_LOOKAHEAD_S * 1000:
                continue  # pressed as the pull ended - nothing left to judge it against

            detail = []
            if c.ctx.defensive[b]:
                detail.append("It also overlapped team defensives.")
            on_cd = [w for w in c.ctx.windows if t < c.t_of(w.start) < t + c.cds.cd_ms[a]]
            worst = min(on_cd, key=lambda w: (not w.deaths, w.min_ehp), default=None)
            if worst:
                what = (f"{len(worst.deaths)} death(s)" if worst.deaths
                        else f"{c.player(worst.lowest_player)} at {worst.min_ehp:.0%}")
                detail.append(f"It was on cooldown at {c.clock(c.t_of(worst.start))} "
                              f"({c.pull_label(worst.pull_idx)}): {what}.")
            severity = 55 if worst and worst.deaths else 35 if worst else 15
            context = [["Needed?", c.need_text(b, ahead.stop - 1) + f" (at the cast and {LOW_VALUE_LOOKAHEAD_S}s after)"],
                       ["Top players", c.top_text(name, pull)]]
            low_value.append(Finding(t, c.pull_label(pull), severity, f"{name} used with no pressure", detail, context))
    return low_value, good


def _overlaps(c: _Ctx) -> list[Finding]:
    """Healing overlap: two or more of YOUR healing cooldowns spent on one damage spike the group got through
    comfortably - one would have done. Worse when the next spike lands while one is still down and someone nearly
    dies or dies. Other players' group defensives are context: your cooldown on top of one only counts (at lower
    weight) when it cost you the next spike - they never weigh like a healer cooldown."""
    out, used, safe = [], set(), []
    hits = sorted((h for s in c.segs for h in s.hits), key=lambda h: h.t)
    my_cds = [(t, a) for a, ts in c.cds.casts.items() for t in ts]
    for h in hits:
        if any(abs(h.t - u) <= OVERLAP_WINDOW_MS for u in used):
            continue  # same spike as one already reported
        mine = [(t, a) for t, a in my_cds if -OVERLAP_WINDOW_MS <= t - h.t <= OVERLAP_WINDOW_MS]
        theirs = [g for g in c.group_defs if -OVERLAP_WINDOW_MS <= g[0] - h.t <= OVERLAP_WINDOW_MS]
        # Your own cooldowns lead: 2+ of yours on one spike is the overlap. Yours on top of another player's group
        # defensive counts too, but weighs less and only when it cost you the next spike.
        own_overlap = len(mine) >= 2
        if not mine or (not own_overlap and not theirs):
            continue
        b = c.tl.bin_of(h.t)
        after = range(b, min(len(c.tl.group_ehp), b + OVERLAP_AFTER_S + 1))
        died = any(h.t <= d[0] <= c.t_of(after.stop) for d in c.run.deaths)
        group_low = min(c.tl.group_ehp[k] for k in after)
        if died or group_low < OVERLAP_SAFE_GROUP or h.min_after < OVERLAP_SAFE_LOWEST:
            continue  # it took everything: not an overlap
        # The next spike while one of your overlapped cooldowns was still down.
        downs = {a: t + c.cds.cd_ms[a] for t, a in mine}
        nxt = next((n for n in hits if n.t > h.t + OVERLAP_WINDOW_MS and any(n.t < back for back in downs.values())), None)
        hurt = None
        if nxt:
            nb = c.tl.bin_of(nxt.t)
            n_after = range(nb, min(len(c.tl.group_ehp), nb + OVERLAP_AFTER_S + 1))
            n_deaths = [d for d in c.run.deaths if nxt.t <= d[0] <= c.t_of(n_after.stop)]
            n_low = min(n_after, key=lambda k: c.tl.min_ehp[k])
            if n_deaths:
                hurt = (nxt, f"{len(n_deaths)} death{'s' if len(n_deaths) > 1 else ''}")
            elif c.tl.min_ehp[n_low] < LETHAL_EHP:
                hurt = (nxt, f"{c.player(c.tl.lowest_player[n_low])} down to {c.tl.min_ehp[n_low]:.0%}")
        planned = all(c.planned_near(a, t) for t, a in mine)
        if planned and not hurt:
            continue  # top players stack these here too
        used.add(h.t)
        yours = [f"{c.cds.name(a)} ({c.clock(t)})" for t, a in sorted(mine)]
        others = [f"{n} from {who} ({c.clock(t)})" for t, n, who in sorted(theirs)]
        pull = c.ctx.pull_of_bin[b]
        detail = [(f"{len(yours)} of your cooldowns on {c.ability(h.ability)} at {c.clock(h.t)}: {', '.join(yours)}."
                   if own_overlap else f"{yours[0]} on {c.ability(h.ability)} at {c.clock(h.t)}, with group defensives "
                                       f"already covering it."),
                  f"The group never dropped below {group_low:.0%} and the lowest player held {h.min_after:.0%} - "
                  + ("one of your cooldowns would likely have been enough." if own_overlap else "it was handled without yours.")]
        if others:
            detail.append(f"Also up (others): {', '.join(others)}.")
        if hurt:
            n, what = hurt
            back = ", ".join(f"{c.cds.name(a)} back at {c.clock(t)}" for a, t in downs.items() if n.t < t)
            detail.append(f"Next spike, {c.ability(n.ability)} at {c.clock(n.t)}: {what} - with {back}.")
        elif nxt:
            detail.append(f"The next spike ({c.ability(nxt.ability)} at {c.clock(nxt.t)}) came while it was "
                          f"still down; it went fine this time.")
        if not hurt:  # harmless this time: your own stacks are summed up below; yours + others' is not worth a line
            if own_overlap:
                safe.append((h.t, f"{c.clock(h.t)} {c.ability(h.ability)}: {' + '.join(yours)}"
                                  + (f" (also up: {', '.join(others)})" if others else "")))
            continue
        died = "death" in hurt[1]
        title = ("Healing overlap left the next spike uncovered" if own_overlap
                 else f"{c.cds.name(mine[0][1])} spent on a covered spike - the next one went short")
        severity = (65 if died else 50) if own_overlap else (45 if died else 35)
        context = [["Top players", c.top_text(c.cds.name(mine[0][1]), pull)]]
        out.append(Finding(h.t, c.pull_label(pull), severity, title, detail, context))
    if len(safe) >= OVERLAP_SAFE_MIN:
        # Timed at the first overlap: whole-run findings at the run start would merge into each other.
        out.append(Finding(safe[0][0], "Whole run", 20, f"{len(safe)} healing overlaps on spikes the group handled", [
            "Two or more of your cooldowns went on one spike while the group stayed healthy - one would likely have "
            "done, and the other would have been ready for the next spike. Nothing went wrong this time:",
            *[s for _, s in safe[:6]], *([f"+{len(safe) - 6} more"] if len(safe) > 6 else [])]))
    return out


def _not_topped(c: _Ctx) -> list[Finding]:
    out = []
    for i, seg in enumerate(c.segs):
        prof = c.profiles[i]
        if not prof:
            continue
        sm = c.seg_models[i]
        model_hits = {h["ability"]: h for h in sm["group_hits"]} if sm else {}
        for ability, hits in _by_ability(seg).items():
            top = prof.hit_pre.get(ability)
            if top is None or prof.hit_count.get(ability, 0) < MIN_HIT_SAMPLES or len(hits) < 2:
                continue
            mine = statistics.mean(h.pre_ehp for h in hits)
            if mine >= top - NOT_TOPPED_MARGIN:
                continue
            near = sum(1 for h in hits if h.min_after < LETHAL_EHP)
            detail = [f"Your group averaged {mine:.0%} before it; peer groups hold {top:.0%}."]
            if near:
                detail.append(f"{near} of the {len(hits)} {c.ability(ability)} hits left someone below {LETHAL_EHP:.0%} HP.")
            mh = model_hits.get(ability)
            if mh and c.spec.maintenance:
                mine_rip = statistics.mean(c.hots[max(0, c.tl.bin_of(h.t) - 1)] for h in hits)
                line = (f"{c.top_label} go in with {mh['riptides_median']:g} {c.spec.maintenance.plural} out "
                        f"(you: {mine_rip:.1f})")
                answer = next(iter(mh["answered_by"].items()), None)
                if answer and answer[1] >= 0.3:
                    line += f" and answer it with {answer[0]} {answer[1]:.0%} of the time"
                detail.append(line + ".")
            severity = 25 + min(30, int((top - mine) * 100)) + (20 if near else 0)
            out.append(Finding(hits[0].t, c.pull_label(i), severity,
                               f"Not topped before {c.ability(ability)} ({len(hits)}x)", detail))
    return out


def _slow_recovery(c: _Ctx) -> list[Finding]:
    out = []
    for i, seg in enumerate(c.segs):
        prof = c.profiles[i]
        if not prof:
            continue
        for ability, hits in _by_ability(seg).items():
            top = prof.hit_recovery.get(ability)
            if top is None or prof.hit_count.get(ability, 0) < MIN_HIT_SAMPLES:
                continue
            # Recovering under more incoming damage (overlapping auras/hits) isn't slow healing.
            clean = [h for h in hits if _recovery_intake(c, h) <= prof.intake_frac_ps * RECOVERY_INTAKE_MAX]
            if len(clean) < 2:
                continue
            hits = clean
            mine = statistics.median(h.recovery_s if h.recovery_s is not None else RECOVERY_CAP_S for h in hits)
            if mine >= top + SLOW_RECOVERY_EXTRA_S and mine >= top * SLOW_RECOVERY_RATIO:
                out.append(Finding(hits[0].t, c.pull_label(i), 30, f"Slow recovery after {c.ability(ability)}", [
                    f"Group took {mine:.0f}s to recover vs {top:.0f}s for peer groups."]))
    return out


LOOSE_MELEE = 0.25  # this share of a pull's mob melee on non-tanks: mobs not held


def _intake_reason(c: _Ctx, a: int, pull) -> str:
    """Why the group took more of one ability, when the log or game data can show it - else nothing (no guess)."""
    if FACTS.ground(a):
        return " - floor effect or frontal (game data): walk out of it"
    through = sum(1 for w in c.went_through if w.get("ability_id") == a and pull.start <= w["t"] <= pull.end)
    if through:
        return f" - {through} cast{'s' if through > 1 else ''} went through that peer groups usually interrupt or stun"
    return ""


def _damage_intake(c: _Ctx) -> list[Finding]:
    """Extra damage taken vs top groups. Compared against top groups with the same
    damage-reducing composition (e.g. an Augmentation Evoker) when there are enough."""
    out = []
    for i, seg in enumerate(c.segs):
        prof = c.profiles[i]
        if not prof or not _comparable(seg, prof):
            continue
        ref, note = _intake_reference(c, prof)
        if seg.intake_frac_ps < ref["intake"] * INTAKE_RATIO:
            continue
        extra = sorted(
            ((a, v - ref["abilities"].get(a, 0.0)) for a, v in seg.ability_intake.items() if a != MELEE_ID),
            key=lambda x: -x[1],
        )[:3]
        detail = ["Healing pressure here is partly damage peer groups avoid or mitigate. Biggest differences:"]
        detail += [
            f"  {c.ability(a)}: {seg.ability_intake[a] * 100:.1f}% group HP/s vs {ref['abilities'].get(a, 0.0) * 100:.1f}%"
            + _intake_reason(c, a, seg.pull) for a, diff in extra if diff > 0
        ]
        melee = [d for d in c.run.damage if seg.pull.start <= d[0] < seg.pull.end and d[2] == MELEE_ID]
        loose = sum(d[3] for d in melee if d[1] not in c.run.tank_ids) / (sum(d[3] for d in melee) or 1)
        if loose >= LOOSE_MELEE:
            detail.append(f"  Melee: {loose:.0%} of it landed on DPS or healer - mobs not held by the tank.")
        if note:
            detail.append(note)
        ratio = seg.intake_frac_ps / ref["intake"] - 1
        out.append(Finding(seg.pull.start, c.pull_label(i), 40,
                           f"Group took {ratio:.0%} more damage than peer groups", detail))
        out[-1].ratio = ratio
    if len(out) > INTAKE_MERGE:  # a group that takes more damage everywhere: one finding, not one per pull
        worst = max(out, key=lambda f: f.ratio)
        lines = [f"{f.pull} ({c.clock(f.t)}): {f.ratio:.0%} more" for f in sorted(out, key=lambda f: f.t)]
        out = [Finding(c.run.start, "Whole run", 45, f"Group took more damage than peer groups on {len(lines)} pulls",
                       [f"Worst: {worst.pull}, {worst.ratio:.0%} more."] + worst.detail[1:] + ["All pulls:"]
                       + [f"  {l}" for l in lines])]
    return out


def _intake_reference(c: _Ctx, prof: Profile) -> tuple[dict, str | None]:
    everyone = {"intake": prof.intake_frac_ps, "abilities": prof.ability_intake}
    mods = c.my_modifiers
    if not prof.segments:
        return everyone, None
    same = [s for s in prof.segments if s.intake_modifiers == mods]
    label = " and ".join(sorted(mods)) if mods else None
    if len(same) >= MIN_SAME_COMP and len(same) < len(prof.segments):
        abilities = {a for s in same for a in s.ability_intake}
        ref = {"intake": statistics.median(s.intake_frac_ps for s in same),
               "abilities": {a: statistics.median(s.ability_intake.get(a, 0.0) for s in same) for a in abilities}}
        who = f"also had {label}" if label else "had no damage-reducing comp effect"
        return ref, f"Compared with the {len(same)} peer groups that {who}."
    if label and not same:
        return everyone, f"No peer group here had {label}; your group should take less than this."
    return everyone, None


def _items(c: _Ctx) -> tuple[list[dict], list[Finding]]:
    """Trinkets, potions and racials vs the comparable top logs: how often, how long they
    sat ready during pressure, and whether they were lined up with the major healing cooldowns."""
    majors_text = c.spec.major_pair_text

    def kind_of(name: str) -> str | None:
        if name in c.my_trinkets:
            return "Trinket"
        if "Potion" in name:
            return "Potion"
        if name in RACIALS:
            return "Racial"
        return None

    mine_by: dict[str, list[int]] = {}
    for t, a, _ in c.run.casts:
        mine_by.setdefault(c.ability(a), []).append(t)
    tops_by = []
    for tr in c.tops:
        d: dict[str, list[int]] = {}
        for t, a, _ in tr.run.casts:
            d.setdefault(tr.run.ability_name(a), []).append(t)
        majors = sorted(t for t, a, _ in tr.run.casts if a in c.spec.healing_cooldowns)
        tops_by.append((tr, d, majors))
    my_majors = sorted(t for ts in c.cds.casts.values() for t in ts)

    rows, out = [], []
    names = {n for n in mine_by if kind_of(n)} | set(c.my_trinkets)
    for name in sorted(names, key=lambda n: (kind_of(n) or "", n)):
        kind = kind_of(name)
        mine = sorted(mine_by.get(name, []))
        users = [(tr, sorted(d[name]), majors) for tr, d, majors in tops_by if d.get(name)]
        if not mine and not users:
            continue  # passive trinket
        gaps = [b - a for ts in [mine] + [u[1] for u in users] for a, b in zip(ts, ts[1:])]
        cd_ms = min(gaps) if gaps and min(gaps) >= MIN_ITEM_CD_S * 1000 else None
        top_uses = (statistics.median(len(ts) / ((tr.run.end - tr.run.start) / 60000) for tr, ts, _ in users)
                    * c.minutes) if users else None
        ready_pressure, dangers, examples = None, 0, []
        if cd_ms:
            def ready(t: int) -> bool:
                last = max((x for x in mine if x <= t), default=None)
                return last is None or t - last >= cd_ms
            idle = [lab in (PRESSURE, *EMERGENCY) and ready(c.t_of(b)) for b, lab in enumerate(c.ctx.labels)]
            ready_pressure = sum(idle)
            examples = [f"{c.clock(c.t_of(s))} ({c.pull_label(c.ctx.pull_of_bin[s])}): ready for {e - s}s "
                        f"while the group was under pressure"
                        for s, e in sorted(_runs_of(idle), key=lambda r: r[0] - r[1])[:3] if e - s >= ITEM_EXAMPLE_S]
            dangers = sum(1 for w in c.ctx.windows
                          if (w.deaths or w.sustained_group_s >= SUSTAINED_S) and ready(c.t_of(w.start)))
        paired = _paired_share(mine, my_majors)
        top_n = sum(len(ts) for _, ts, _ in users)
        top_paired = (sum(sum(1 for t in ts if _near(t, majors)) for _, ts, majors in users) / top_n) if top_n else None
        rows.append({"name": name, "kind": kind, "uses": len(mine),
                     "top_uses": round(top_uses, 1) if top_uses is not None else None,
                     "top_users": len(users), "top_count": len(c.tops),
                     "cd_s": round(cd_ms / 1000) if cd_ms else None, "ready_pressure_s": ready_pressure,
                     "ready_examples": examples,
                     "paired": paired, "top_paired": top_paired})

        if kind != "Trinket" or len(users) < 2:
            continue
        top_line = f"{len(users)} of {len(c.tops)} comparable top {c.spec.plural} run it"
        if top_paired is not None:
            top_line += f"; {top_paired:.0%} of their uses line up with {majors_text}"
        if top_uses and len(mine) < top_uses * ITEM_RATE_RATIO and (ready_pressure or 0) >= ITEM_MIN_READY_PRESSURE_S:
            out.append(Finding(c.run.start, "Whole run", 40, f"{name} used less than your peers", [
                f"You used it {len(mine)} times; peer healers who run it use it ~{top_uses:.0f} times in a run this long."],
                [["Ready?", f"Sat ready through {ready_pressure}s of pressure (cooldown ~{cd_ms // 1000}s)."],
                 ["Needed?", f"{dangers} group-wide dangerous moment(s) while it was available."],
                 ["Peers", top_line + "."]]))
        elif (top_paired is not None and paired is not None and len(mine) >= 3
              and top_paired >= 0.5 and paired <= top_paired - 0.3):
            out.append(Finding(c.run.start, "Whole run", 30, f"{name} not lined up with your cooldowns", [
                f"{paired:.0%} of your uses were within {PAIR_WINDOW_MS // 1000}s of {majors_text}."],
                [["Peers", top_line + "."]]))
    return rows, out


def _spell_choice(c: _Ctx) -> tuple[dict, list[Finding]]:
    """Single-target vs group heal (Healing Wave vs Chain Heal) judged by the situation at each cast,
    against the same situations in the comparable top logs. Only stretches that clearly mattered are
    flagged: a run of group heals while one player was dying, or single heals through a group spike."""
    mine, pair = c.heal_casts, c.spec.spell_pair
    top_all = [h for tr in c.tops for h in tr.heal_casts]
    if not mine or not pair:
        return {}, []
    one, grp = pair.single, pair.group
    table = {"you": choice_table(mine, pair), "top": choice_table(top_all, pair) if top_all else None,
             "buffs": {"you": buff_table(mine, c.spec), "top": buff_table(top_all, c.spec) if top_all else None}}
    top = table["top"] or {}
    out = []

    # Whole run: picking the single heal where top players group-heal, when your own group heals
    # in that situation clearly healed more per cast.
    labels = {"two": "2 players", "group": "3+ players"}
    for sit, label in labels.items():
        you, them = table["you"][sit], top.get(sit, {})
        if (you["casts"] < SPELL_MIN_CASTS or them.get("ch_share") is None or you["ch_share"] is None
                or you["ch_effective"] is None or you["hw_effective"] is None):
            continue
        gain = you["ch_effective"] / you["hw_effective"] - 1
        if them["ch_share"] - you["ch_share"] < SPELL_SHARE_GAP or gain < SPELL_CH_GAIN:
            continue
        out.append(Finding(c.run.start, "Whole run", 35, f"{one} over {grp} with {label} injured", [
            f"Your {grp}s in that situation healed {gain:.0%} more per cast than your {one}s "
            f"({you['ch_effective'] / 1000:.0f}k vs {you['hw_effective'] / 1000:.0f}k effective), "
            f"at {you['ch_overheal']:.0%} overheal.",
            f"Your mana averaged {you['mana']:.0%} during those heals - {grp} costs more, so this is the trade-off."
            if you.get("mana") is not None else f"{grp} costs more mana - that is the trade-off."],
            [["Situation", f"{you['casts']} heals cast with {label} below 80%."],
             ["Peers", f"{c.peer_label} use {grp} {them['ch_share']:.0%} of the time here "
                          f"(you: {you['ch_share']:.0%})."]]))

    # Under a healing buff: top players switch spells (e.g. Chain Heal during Ascendance) and you don't.
    buffs_you, buffs_top = table["buffs"]["you"], table["buffs"]["top"] or {}
    for name, you in buffs_you.items():
        them = buffs_top.get(name, {})
        if you["casts"] < BUFF_MIN_CASTS or you["ch_share"] is None or them.get("ch_share") is None:
            continue
        if them["ch_share"] - you["ch_share"] < BUFF_SHARE_GAP:
            continue
        # Evidence the switch pays off must agree: your heals under the buff AND the top players'.
        favours_ch = [s["ch_effective"] > s["hw_effective"] for s in (you, them)
                      if s.get("ch_effective") and s.get("hw_effective")]
        if not favours_ch or not all(favours_ch):
            continue
        out.append(Finding(c.run.start, "Whole run", 40, f"{grp} underused during {name}", [
            f"During {name}, {grp} healed more per cast than {one} - yours "
            f"{_k(you['ch_effective'])} vs {_k(you['hw_effective'])}, peer healers' "
            f"{_k(them['ch_effective'])} vs {_k(them['hw_effective'])}."],
            [["Situation", f"{you['casts']} heals cast with {name} up."],
             ["Peers", f"{c.peer_label} use {grp} {them['ch_share']:.0%} of the time during {name} "
                          f"(you: {you['ch_share']:.0%})."]], rule="buff_choice"))

    # Group heals while a single player was in danger (top players mostly single-heal here).
    top_one = top.get("one", {}).get("ch_share")
    if top_one is not None and top_one <= CH_SINGLE_TOP_MAX:
        for stretch in _stretches([h for h in mine if h.spell == grp and h.situation in ("none", "one")]):
            if (statistics.mean(h.overheal_share for h in stretch) < CH_SINGLE_OVERHEAL
                    or min(h.lowest for h in stretch) >= HELD_MIN_EHP):
                continue
            t0, t1 = stretch[0].t, stretch[-1].t
            b0, b1 = c.tl.bin_of(t0), c.tl.bin_of(t1)
            low = min(range(b0, b1 + 1), key=lambda b: c.tl.min_ehp[b])
            out.append(Finding(t0, c.pull_label(c.ctx.pull_of_bin[b0]), 40,
                               f"{len(stretch)} {grp}s while only {c.player(c.tl.lowest_player[low])} needed healing", [
                f"{statistics.mean(h.overheal_share for h in stretch):.0%} of those {grp}s overhealed; "
                f"{c.player(c.tl.lowest_player[low])} dropped to {c.tl.min_ehp[low]:.0%} meanwhile."],
                [["Situation", f"One player injured, the rest topped ({c.clock(t0)}-{c.clock(t1)})."],
                 ["Peers", f"With one player injured, {c.peer_label.lower()} use {grp} {top_one:.0%} of the time "
                              f"(you: {table['you']['one']['ch_share']:.0%}) - {one} lands the big single heal."]]))

    # Single heals through a group spike (top players mostly group-heal here).
    top_group = top.get("group", {}).get("ch_share")
    if top_group is not None and top_group >= HW_GROUP_TOP_MIN:
        hw = [h for h in mine if h.spell == one and h.situation == "group" and not h.after_swiftness]
        for stretch in _stretches(hw):
            t0, t1 = stretch[0].t, stretch[-1].t
            if any(h.spell == grp and t0 <= h.t <= t1 for h in mine):
                continue
            b0, b1 = c.tl.bin_of(t0), c.tl.bin_of(t1)
            sustained = sum(1 for b in range(b0, b1 + 2) if low_count(c.tl, b) >= GROUP_LOW_COUNT)
            if sustained < SUSTAINED_S:
                continue
            out.append(Finding(t0, c.pull_label(c.ctx.pull_of_bin[b0]), 40,
                               f"{len(stretch)} {one}s through group-wide damage", [
                f"{min(h.injured for h in stretch)}+ players were injured at every cast and 2+ sat under 50% for "
                f"{sustained}s; no {grp} in between."],
                [["Situation", f"Group-wide damage ({c.clock(t0)}-{c.clock(t1)})."],
                 ["Peers", f"With 3+ players injured, {c.peer_label.lower()} use {grp} {top_group:.0%} of the time "
                              f"(you: {table['you']['group']['ch_share']:.0%})."]]))
    return table, out


def _k(v: float | None) -> str:
    return "-" if v is None else f"{v / 1000:.0f}k"


def _stretches(casts: list[HealCast]) -> list[list[HealCast]]:
    """Runs of at least SPELL_STRETCH_MIN casts, each within SPELL_STRETCH_GAP_MS of the previous."""
    out, cur = [], []
    for h in sorted(casts, key=lambda h: h.t):
        if cur and h.t - cur[-1].t > SPELL_STRETCH_GAP_MS:
            if len(cur) >= SPELL_STRETCH_MIN:
                out.append(cur)
            cur = []
        cur.append(h)
    if len(cur) >= SPELL_STRETCH_MIN:
        out.append(cur)
    return out


def _runs_of(flags: list[bool], gap: int = 2) -> list[tuple[int, int]]:
    """(start, end) bin ranges where flags are true, bridging gaps of up to `gap` bins."""
    out, start, last = [], None, None
    for b, f in enumerate(flags):
        if not f:
            continue
        if start is None or b - last > gap + 1:
            if start is not None:
                out.append((start, last + 1))
            start = b
        last = b
    if start is not None:
        out.append((start, last + 1))
    return out


def _near(t: int, times: list[int]) -> bool:
    return any(abs(t - x) <= PAIR_WINDOW_MS for x in times)


def _paired_share(times: list[int], majors: list[int]) -> float | None:
    return sum(1 for t in times if _near(t, majors)) / len(times) if times else None


def _riptide_under_pressure(c: _Ctx) -> list[Finding]:
    """Maintenance-HoT uptime and early refreshes in the busiest moments vs top players."""
    out = []
    if not c.spec.maintenance:
        return out
    hot = c.spec.maintenance.name
    for i, seg in enumerate(c.segs):
        prof = c.profiles[i]
        if (not prof or prof.hot_pressure_uptime is None or seg.hot_pressure_uptime is None
                or len(seg.pressure_bins) < MIN_PRESSURE_BINS):
            continue
        if not _comparable(seg, prof):
            continue
        hurt = seg.median_ehp < prof.target_ehp - 0.03 or any(
            w.pull_idx == i and (w.deaths or w.sustained_group_s >= SUSTAINED_S) for w in c.ctx.windows)
        if not hurt:
            continue  # the group was fine - HoT habits here cost nothing
        mine, top = seg.hot_pressure_uptime, prof.hot_pressure_uptime
        low_uptime = mine < top - RIPTIDE_GAP
        wasted, top_wasted = seg.hot_wasted_pressure_s, prof.hot_wasted_pressure_s
        over_refresh = wasted >= 2 * top_wasted and wasted - top_wasted >= RIPTIDE_WASTE_S
        if not low_uptime and not over_refresh:
            continue
        detail = []
        if low_uptime:
            detail.append(f"During the busiest moments of this pull {hot} was out {mine:.0%} of the time; "
                          f"{c.top_label.lower()}: {top:.0%}.")
        if over_refresh:
            detail.append(f"Early refreshes in those moments wasted {wasted:.0f}s of {hot} "
                          f"vs {top_wasted:.0f}s for {c.top_label.lower()} - those GCDs could have gone elsewhere.")
        severity = 20 + min(25, int(max(0.0, top - mine) * 100)) + (10 if over_refresh else 0)
        out.append(Finding(seg.pull.start, c.pull_label(i), severity, f"{hot} under pressure", detail))
    return out


def _requirements(c: _Ctx) -> list[dict]:
    rows = []
    for i, seg in enumerate(c.segs):
        prof = c.profiles[i]
        if seg.duration_s < MIN_PULL_S:
            continue
        rows.append({
            "pull": c.pull_label(i),
            "duration_s": seg.duration_s,
            "group_hp": seg.median_ehp,
            "top_group_hp": prof.target_ehp if prof else None,
            "intake": seg.intake_hps,
            "top_intake": prof.intake_hps if prof else None,
            "healing": seg.healing_hps,
            "top_healing": prof.healing_hps if prof else None,
            "matched": prof.n if prof else 0,
        })
    return rows


def _recovery_intake(c: _Ctx, h) -> float:
    b0 = c.tl.bin_of(h.t) + 1
    span = int(h.recovery_s or RECOVERY_CAP_S)
    bins = range(b0, min(len(c.tl.intake_frac), b0 + span))
    return sum(c.tl.intake_frac[b] for b in bins) / len(bins) if bins else 0.0


def _comparable(seg: SegmentMetrics, prof: Profile) -> bool:
    """Trash pulls are matched by enemy set; if the damage scale is far off, the route
    pulled a different amount and the comparison is meaningless."""
    if not prof.intake_frac_ps:
        return False
    ratio = seg.intake_frac_ps / prof.intake_frac_ps
    return INTAKE_SANE[0] <= ratio <= INTAKE_SANE[1]


def _self_danger(c: _Ctx) -> list[Finding]:
    """The healer low for several seconds with a personal defensive ready. Group cooldowns are
    the wrong answer to being focused; a personal is the right one."""
    me = c.run.healer_id
    series = c.tl.player_ehp.get(me)
    personals = c.spec.personal_cooldowns
    if not series or not personals:
        return []
    personal = CooldownTracker(c.run, personals)
    out, b = [], 0
    while b < len(series):
        if not 0 < series[b] < SELF_DANGER_EHP:
            b += 1
            continue
        start = b
        while b < len(series) and 0 < series[b] < SELF_DANGER_EHP:
            b += 1
        if b - start < SELF_DANGER_S:
            continue
        t0, t1 = c.t_of(start), c.t_of(b)
        if any(d[1] == me and t0 - REZ_GRACE_MS <= d[0] <= t0 for d in c.run.deaths):
            continue  # low HP right after a death / self-res (Reincarnation), not incoming damage
        ready = [a for a in personals
                 if a not in personal.casts or (personal.ready(a, t0) and not personal.used_between(a, t0 - 5000, t1))]
        if not ready:
            continue
        hitters = {}
        for d in c.run.damage:
            if d[1] == me and t0 - 3000 <= d[0] <= t1:
                hitters[d[2]] = hitters.get(d[2], 0) + d[3] + d[4]
        if not hitters:
            continue
        top_hits = ", ".join(c.ability(a) for a, _ in sorted(hitters.items(), key=lambda x: -x[1])[:3])
        names = ", ".join(personals[a][0] for a in ready)
        died = any(d[1] == me and t0 <= d[0] <= t1 + 2000 for d in c.run.deaths)
        detail = [f"Damage on you: {top_hits}."]
        if c.spec.personal_habit:
            detail.append(f"{c.top_label} {c.spec.personal_habit}.")
        out.append(Finding(t0, c.pull_label(c.ctx.pull_of_bin[start]), 50 + (20 if died else 0),
                           f"You were under {SELF_DANGER_EHP:.0%} for {b - start}s with {names} ready", detail))
    return out


def _by_ability(seg: SegmentMetrics) -> dict:
    out: dict[int, list] = {}
    for h in seg.hits:
        out.setdefault(h.ability, []).append(h)
    return out
