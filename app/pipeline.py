"""One full analysis of a run, as a JSON-ready dict. Shared by the CLI and the web app.

Results are cached per run + rules version, so reopening a key is instant. Bump
RULES_VERSION when findings logic changes.
"""
import statistics
from typing import Callable

from app.baseline import comparable_fights
from app.composition import describe, intake_modifiers, load_effects
from app.context import build_context
from app.context_model import load_dungeon
from app.chains import OUTCOME_S, Chain, find_chains
from app.findings import Finding, TopRun, analyze
from app.metrics import hot_refreshes, hots_active
from app.reports import get_fights, get_players, get_trinkets, parse_report_url, pick_fight
from app.runs import fetch_run
from app.segments import all_segments, build_profile, match_pull, segment_metrics
from app.avoidable import Avoidance, build as build_avoidance
from app.auras import dispellable, fetch_auras, learn_self_clears, top_debuff_handling
from app.deaths import (MAX_RECAPS, _applies, ability_schools, clear_tools, fetch_death_events, fetch_group_casts,
                        load_defensives, recap_death)
from app.context import CooldownTracker
from app.defense_spans import active_at, fetch_defense_spans
from app.healer_state import fetch_healer_state
from app.phases import fetch_boss_casts, phase_breakdown
from app.quality import check as quality_check
from app.spells import fetch_spell_heals, heal_casts
from app.verdicts import _section, items_verdict, phases_verdict, pulls_verdict, spell_verdict
from app.specs import SPECS, healers_in
from app.timeline import build_timeline
from app.wcl import WCLClient

RULES_VERSION = 34
QUALITY_BACKFILL = 6  # extra ranked logs to try when some fail the data-quality gate
CURVE_POINTS = 60
LUST_SPELLS = {"Heroism", "Bloodlust", "Time Warp", "Fury of the Aspects", "Primal Rage", "Ancient Hysteria",
               "Netherwinds", "Drums of the Maelstrom"}
UNCOVERED_MIN_HIT = 0.18  # only flag "no defensives up" on hits this big (share of max HP per player)
SOFTEN_RULES = {"not_topped", "slow_recovery", "phase_gap", "damage_intake", "riptide_pressure"}
SOFTEN_SHARE = 0.2  # avoidable share of the damage around a healing finding that softens it
SOFTEN_WINDOW_MS = 15_000


class AnalysisError(ValueError):
    pass


def pick_healer(players, player_name: str | None):
    """(player, spec): the named player if they heal on a supported spec, else the first supported healer."""
    healers = healers_in(players)
    named = [h for h in healers if player_name and h[0].name.lower() == player_name.lower()]
    if named or healers:
        return (named or healers)[0]
    supported = ", ".join(s.label for s in SPECS)
    raise AnalysisError(f"No supported healer in that run (supported: {supported}).")


def cache_key(code: str, fight_id: int, player: str, top: int) -> str:
    return f"analysis:v{RULES_VERSION}:{code}:{fight_id}:{player.lower()}:{top}"


def run_analysis(client: WCLClient, url: str, player_name: str | None = None, top: int = 8,
                 progress: Callable[[str], None] = lambda msg: None, use_cache: bool = True) -> dict:
    code, fight_id = parse_report_url(url)
    fight = pick_fight(get_fights(client, code), fight_id)
    me, spec = pick_healer(get_players(client, fight), player_name)

    key = cache_key(code, fight.id, me.name, top)
    if use_cache:
        cached = client.get_cached(key)
        if cached:
            return cached

    progress(f"Fetching your run ({fight.name} +{fight.keystone_level or 0})...")
    effects = load_effects()
    my_players = get_players(client, fight)
    my_mods = frozenset(intake_modifiers(my_players, effects))
    run = fetch_run(client, fight, me.id, spec)
    tl = build_timeline(run)
    segs = all_segments(run, tl, spec)

    my_state = fetch_healer_state(client, fight, run, spec)
    my_boss_casts = fetch_boss_casts(client, fight, run)

    progress("Finding comparable top logs...")
    tops, top_boss, candidates, skipped = [], [], [], []
    # Extra candidates so logs failing the data-quality gate can be replaced from further down.
    for f, p in comparable_fights(client, fight, spec.class_name, spec.spec_name, top + QUALITY_BACKFILL):
        if len(tops) >= top:
            break
        progress(f"Fetching top log {len(tops) + 1} of {top} ({p.name} +{f.keystone_level})...")
        other = fetch_run(client, f, p.id, spec)
        otl = build_timeline(other)
        ok, reasons = quality_check(other.damage, other.player_ids, f.duration_s, fight.duration_s, otl.group_ehp)
        if not ok:
            skipped.append({"player": p.name, "key": f.keystone_level, "reasons": reasons})
            continue
        candidates.append((f, p))
        state = fetch_healer_state(client, f, other, spec)
        top_run = TopRun(other, get_trinkets(client, f).get(p.id, []),
                         frozenset(intake_modifiers(get_players(client, f), effects)),
                         heal_casts(other, otl, fetch_spell_heals(client, f, other, spec), state, spec))
        tops.append((top_run, otl))
        top_boss.append((other, otl, fetch_boss_casts(client, f, other)))

    progress("Analyzing...")
    top_rip = [(hots_active(tr.run, otl) if spec.maintenance else None, hot_refreshes(tr.run, spec))
               for tr, otl in tops]
    profiles = []
    for seg in segs:
        matched = []
        for (tr, otl), (rip, refreshes) in zip(tops, top_rip):
            p = match_pull(seg.pull, tr.run)
            if p:
                m = segment_metrics(tr.run, otl, p, rip, refreshes)
                m.intake_modifiers = tr.intake_modifiers
                matched.append(m)
        profiles.append(build_profile(matched))

    model = load_dungeon(run.dungeon, spec)
    ctx = build_context(run, tl, segs, profiles)
    my_trinkets = get_trinkets(client, fight).get(me.id, [])
    my_heals = heal_casts(run, tl, fetch_spell_heals(client, fight, run, spec), my_state, spec)
    phases, phase_rows = phase_breakdown(run, tl, my_boss_casts, top_boss, spec)

    def clock(t: int) -> str:
        s = max(0, (t - run.start) // 1000)
        return f"{s // 60}:{s % 60:02d}"

    group_casts = fetch_group_casts(client, fight, {p.id for p in my_players})
    progress("Tagging avoidable damage...")
    avoid = build_avoidance(client, fight, run, [(f, tr.run) for (tr, _), (f, _) in zip(tops, candidates)],
                            group_casts)
    death_recaps = _death_recaps(client, fight, run, my_players, my_state, [tr.run for tr, _ in tops],
                                 candidates, clock, progress, spec)
    for r in death_recaps:
        tag = avoid.tag_by_name(run, r["cause"]["name"], r["t"])
        r["avoid"] = {"label": tag.label, "line": tag.line()} if tag else None
    result = analyze(run, tl, segs, profiles, ctx, model, [tr for tr, _ in tops], my_trinkets, my_mods,
                     my_heals, phases, phase_rows, my_state, death_recaps, spec=spec)
    chains = find_chains(run, tl, fetch_auras(client, fight, run.player_ids), my_state, tops, group_casts, spec)
    result.findings += _chain_findings(run, chains, clock)
    _soften(result.findings, run, avoid, death_recaps, clock)
    comp_notes = [
        f"{mod}: in your group; {sum(1 for tr, _ in tops if mod in tr.intake_modifiers)} of {len(tops)} "
        f"top groups had one too." for mod in sorted(my_mods)
    ]

    def as_dict(f):
        return {"clock": clock(f.t), "t_s": round((f.t - run.start) / 1000, 1), "pull": f.pull,
                "severity": f.severity, "title": f.title, "detail": f.detail, "context": f.context, "rule": f.rule,
                "short": f.short, "fix": f.fix}

    phase_out = [{**{k: v for k, v in r.items() if k not in ("start", "end")},
                  "pull": f"Pull {r['pull_idx'] + 1}: {run.pulls[r['pull_idx']].name}",
                  "clock": clock(r["start"])} for r in phase_rows]
    phase_v = phases_verdict(phase_out)
    verdicts = {
        "spells": spell_verdict(result.spell_choice, my_heals, clock, spec),
        "items": items_verdict(result.items, spec),
        "phases": phase_v,
        "pulls": pulls_verdict(result.requirements, phase_v),
    }
    # Every chain pull, one line each - an action only when it went in risky and then hurt.
    if chains and verdicts["pulls"]:
        verdicts["pulls"]["lines"] += [{"text": "⛓ " + _chain_line(run, c), "action": c.risky and c.hurt,
                                        "examples": [f"At {clock(c.t)}"]} for c in chains]
        risky = sum(1 for c in chains if c.risky)
        ok_head = (verdicts["pulls"]["headline"].rstrip(".") + f" · {len(chains)} chain pull{'s' if len(chains) > 1 else ''}"
                   + (f", {risky} went in risky." if risky else ", none risky."))
        verdicts["pulls"] = _section(verdicts["pulls"]["lines"], ok_head if verdicts["pulls"]["ok"] else
                                     verdicts["pulls"]["headline"], "pulls")
    out = {
        "run": {
            "code": code,
            "fight_id": fight.id,
            "dungeon": fight.name,
            "level": fight.keystone_level,
            "timed": bool(fight.kill),
            "player": me.name,
            "duration_min": round(fight.duration_s / 60, 1),
            "deaths": len(run.deaths),
        },
        "spec": {"key": spec.key, "label": spec.label, "plural": spec.plural,
                 "single": spec.spell_pair.single if spec.spell_pair else None,
                 "group": spec.spell_pair.group if spec.spell_pair else None,
                 "maintenance": spec.maintenance.plural if spec.maintenance else None,
                 "majors": spec.major_pair_text},
        "compared": {
            "count": len(tops),
            "levels": sorted({tr.run.keystone_level for tr, _ in tops}),
            "model": model.runs_label() if model else None,
            "skipped": skipped,
        },
        "group": _with_brings(describe(my_players, effects), my_players, run, segs,
                              fetch_group_casts(client, fight, {p.id for p in my_players})),
        "comp_notes": comp_notes,
        "trinkets": my_trinkets,
        "items": result.items,
        "spell_choice": result.spell_choice,
        "findings": [as_dict(f) for f in result.findings],
        "good_calls": [as_dict(f) for f in result.good_calls],
        "requirements": result.requirements,
        "phases": phase_out,
        "verdicts": verdicts,
        "deaths": death_recaps,
        "timeline": _timeline(run, tl, segs, profiles, phases, my_state, my_trinkets, result, tops, death_recaps,
                              my_players, fetch_defense_spans(client, fight, run, my_players, load_defensives()),
                              group_casts, avoid, chains, spec),
    }
    client.set_cached(key, out)
    return out


def _death_recaps(client, fight, run, players, state, top_runs, candidates, clock, progress, spec) -> list[dict]:
    """Recap for each death: cause, what was ready (group / victim / healer), the debuff and
    dispel story, the victim's last casts, and how top players handle the same debuff."""
    deaths = [d for d in sorted(run.deaths, key=lambda d: d[0])
              if d[2] or d[0] < run.end - 10_000][:MAX_RECAPS]  # skip end-of-key resets
    if not deaths:
        return []
    progress("Building death recaps...")
    defensives = load_defensives()
    tools = clear_tools(defensives)
    group_casts = fetch_group_casts(client, fight, {p.id for p in players})
    schools = ability_schools(client, fight)
    auras = fetch_auras(client, fight, {p.id for p in players})
    top_auras = [(tr, fetch_auras(client, f, tr.player_ids)) for tr, (f, _) in zip(top_runs, candidates)]
    can_dispel = dispellable(auras, *[a for _, a in top_auras])
    # Which own tools clear which debuff - from this run and the top healers' runs.
    self_clears = learn_self_clears(auras, group_casts, run.ability_name, tools)
    for tr, a in top_auras:
        top_casts = [[t, tr.healer_id, aid] for t, aid, _ in tr.casts]
        for aid, names in learn_self_clears(a, top_casts, tr.ability_name, tools).items():
            self_clears.setdefault(aid, set()).update(names)
    debuff_names = {run.ability_name(d[1]) for t, victim, _ in deaths for d in auras.debuffs
                    if d[0] == victim and t - 10_000 <= d[3] and d[2] <= t}
    handling = top_debuff_handling(top_auras, debuff_names, tools, spec.plural)
    out = []
    for t, victim, killing in deaths:
        ev = fetch_death_events(client, fight, victim, t)
        r = recap_death(run, players, group_casts, defensives, ev, t, victim, killing, schools,
                        auras, can_dispel, self_clears, handling, state.procs() if state else None)
        r["clock"] = clock(t)
        r["pull"] = next((f"Pull {i + 1}: {p.name}" for i, p in enumerate(run.pulls) if p.start <= t <= p.end), "")
        r["short"], r["fix"] = _death_short(r)
        out.append(r)
    return out


def _pull_label(run, i: int) -> str:
    p = run.pulls[i]
    return f"Pull {i + 1}: {p.name}" + (" (boss)" if p.is_boss else "")


def _chain_text(c: Chain) -> list[str]:
    """Hover lines for a chain pull."""
    lines = [f"Chain pull: {c.spill[0]} still hitting" if c.spill else "Chain pull: no break after the last pack"]
    lines += [f"⚠ {r}" for r in c.risks] if c.risky else (["Nothing risky going in"] + c.risks)
    if c.top_chained:
        lines.append(f"Top groups chained here in {c.top_chained[0]} of {c.top_chained[1]} runs")
    lines.append(f"Next {OUTCOME_S}s: {c.deaths} death{'s' if c.deaths != 1 else ''}, lowest player {c.min_lowest:.0%}, "
                 f"group down to {c.min_group:.0%}")
    return lines


def _chain_line(run, c: Chain) -> str:
    """One line per chain: 'Pull 7 → 8 · chained · nothing risky · top groups chain here 5/8'."""
    parts = [f"Pull {c.prev_idx + 1} → {c.idx + 1} ({run.pulls[c.idx].name})", "chained",
             "; ".join(c.risks) if c.risky else "nothing risky going in"]
    if c.top_chained:
        parts.append(f"top groups chain here {c.top_chained[0]}/{c.top_chained[1]}")
    if c.hurt:
        parts.append(f"{c.deaths} death(s), lowest {c.min_lowest:.0%} in the next {OUTCOME_S}s" if c.deaths
                     else f"lowest player {c.min_lowest:.0%} in the next {OUTCOME_S}s")
    return " · ".join(parts)


def _chain_findings(run, chains: list[Chain], clock) -> list[Finding]:
    """A finding only for chains that went in risky and then hurt."""
    out = []
    for c in chains:
        if not (c.risky and c.hurt):
            continue
        b = run.pulls[c.idx]
        rare = c.top_chained and c.top_chained[0] * 2 < c.top_chained[1]
        fix = ("Clear the DoTs before the next pack" if c.dots else
               "Wait for a reset" if rare else "Top up / keep a cooldown for it")
        detail = [f"{run.pulls[c.prev_idx].name} → {b.name} at {clock(c.t)} with no break"
                  + (f" ({', '.join(c.spill[:2])} still hitting after the pull)." if c.spill else ".")]
        detail += [r[0].upper() + r[1:] + "." for r in c.risks]
        if c.dots:
            detail.append("Dispel or self-clear what you can before the next pack, or ask for a few seconds.")
        context = [["Needed?", f"Next {OUTCOME_S}s: {c.deaths} death(s), lowest player {c.min_lowest:.0%}, "
                               f"group down to {c.min_group:.0%}"]]
        if c.cds_down:
            context.append(["Ready?", f"{' and '.join(c.cds_down)} on cooldown when the pack came in"])
        if c.top_chained:
            context.append(["Top runs", f"chained here in {c.top_chained[0]} of {c.top_chained[1]} runs"
                            + (f"; their group at {c.top_hp:.0%} starting this pull" if c.top_hp is not None else "")])
        sev = 45 + (20 if c.deaths else 0) + (10 if rare else 0) + (5 if c.dots else 0)
        out.append(Finding(c.t, _pull_label(run, c.idx), sev, f"Risky chain pull into {b.name}", detail, context,
                           "chain_pull", short=f"Risky chain pull into {b.name}", fix=fix))
    return out


def _soften(findings: list, run, avoid: Avoidance, recaps: list[dict], clock) -> None:
    """Healing findings where much of the damage around them was avoidable get a lower score and
    say so; deaths to avoidable damage say so too."""
    by_clock = {r["clock"]: r for r in recaps}
    for f in findings:
        if f.rule == "death":
            r = by_clock.get(clock(f.t))
            if r and r.get("avoid"):
                f.context.append(["Avoidable?", r["avoid"]["line"]])
            continue
        if f.rule not in SOFTEN_RULES:
            continue
        pull = next((p for p in run.pulls if p.start <= f.t <= p.end), None)
        if not pull:
            continue
        lo, hi = ((pull.start, pull.end) if f.rule == "phase_gap" else
                  (max(pull.start, f.t - SOFTEN_WINDOW_MS), min(pull.end, f.t + SOFTEN_WINDOW_MS)))
        avoidable, total, names = avoid.damage_in(run, lo, hi)
        if not total or avoidable / total < SOFTEN_SHARE:
            continue
        top = ", ".join(n for n, _ in sorted(names.items(), key=lambda x: -x[1])[:3])
        f.detail.insert(0, f"Group took avoidable {top} ({avoidable / total:.0%} of the damage here) - "
                           f"not a healing problem.")
        f.context.append(["Avoidable?", f"{avoidable / total:.0%} of the damage {clock(lo)}-{clock(hi)}: {top}"])
        f.severity = int(f.severity * 0.6)
    findings.sort(key=lambda f: -f.severity)


def _with_brings(group: list[dict], players, run, segs, group_casts) -> list[dict]:
    """Group/external cooldowns each member brings: how often pressed, and how many big group hits
    one of those presses was up for (pressed within its duration before the hit)."""
    defensives = load_defensives()
    big = [h.t for seg in segs for h in seg.hits if h.avg_frac >= UNCOVERED_MIN_HIT]
    by_name = {p.name: p for p in players}
    for m in group:
        p, items = by_name.get(m["name"]), []
        for name, d in defensives.items():
            if not p or d["scope"] == "personal" or d["effect"].get("escape") or not _applies(d, p):
                continue
            times = [t for t, src, aid, _ in group_casts if src == p.id and run.ability_name(aid) == name]
            on_hits = sum(1 for h in big if any(0 <= h - t <= d["duration"] * 1000 for t in times))
            items.append({"name": name, "scope": d["scope"], "casts": len(times), "on_big_hits": on_hits})
        m["brings"] = items
        m["big_hits"] = len(big)
    return group


def _death_short(r: dict) -> tuple[str, str]:
    """Collapsed death card: 'Died in Bloodthorn Roots (9.5s)' and one fix."""
    story, cause = r.get("story"), r["cause"]["name"]
    short = (f"Died in {cause} ({story['on_for']:.1f}s)" if story and story["debuff"] == cause
             else f"Died to {cause}")
    every = [i for k in ("victim", "healer", "group") for i in r["ready"][k] if not i.get("escape")]
    savers = [i for i in every if i["status"] == "ready" and i.get("verdict") == "lived"]
    if story and story.get("fix"):
        fix = story["fix"]
    elif savers:
        fix = f"Use {savers[0]['name']}"
    else:
        fix = ""
    return short, fix


def _top_curve(seg, tops) -> list[list[float]] | None:
    """What top groups' HP actually did through the same pull: each matched pull resampled
    to CURVE_POINTS steps of its own length, then [position, p25, median, p75] per step."""
    series = []
    for tr, otl in tops:
        p = match_pull(seg.pull, tr.run)
        if not p:
            continue
        bins = list(otl.slice(p.start, p.end))
        if len(bins) >= 5:
            series.append([otl.group_ehp[bins[int(i * (len(bins) - 1) / (CURVE_POINTS - 1))]] for i in range(CURVE_POINTS)])
    if len(series) < 2:
        return None
    out = []
    for i in range(CURVE_POINTS):
        vals = sorted(s[i] for s in series)
        q = lambda f: vals[min(len(vals) - 1, int(f * len(vals)))]
        out.append([round(i / (CURVE_POINTS - 1), 3), round(q(0.25), 3), round(statistics.median(vals), 3), round(q(0.75), 3)])
    return out


def _timeline(run, tl, segs, profiles, phases, state, trinkets, result, tops=(), recaps=(), players=(),
              spans=(), group_casts=(), avoid: Avoidance | None = None, chains=(), spec=None) -> dict:
    """Everything the page needs to draw HP over time: cooldowns, deaths, hits, findings - each
    with hover details - plus each party member's active defensives."""
    rel = lambda t: round((t - run.start) / 1000, 1)
    clock = lambda t: f"{max(0, t - run.start) // 60000}:{max(0, t - run.start) // 1000 % 60:02d}"
    pct = lambda v: f"{v:.0%}" if v is not None else "-"
    by_id = {p.id: p for p in players}
    pname = lambda pid: by_id[pid].name if pid in by_id else "totem/pet"

    def where(t: int) -> str:
        i = next((k for k, p in enumerate(run.pulls) if p.start <= t <= p.end), None)
        if i is None:
            return "between pulls"
        ph = next((ph.name for ph in phases.get(i, []) if ph.start <= t < ph.end), None)
        return f"Pull {i + 1}: {run.pulls[i].name}" + (f" · {ph}" if ph else "")

    def hp_line(t: int) -> str:
        b = tl.bin_of(t)
        return f"Group {pct(tl.group_ehp[b])} · lowest {pct(tl.min_ehp[b])}"

    healing, personals = spec.healing_cooldowns, spec.personal_cooldowns
    cds = CooldownTracker(run, healing)
    markers = []
    for t, aid, target in run.casts:
        name = run.ability_name(aid)
        kind = ("cooldown" if aid in healing else "personal" if aid in personals
                else "trinket" if name in trinkets else "potion" if "Potion" in name else None)
        if not kind:
            continue
        tip = [f"{name} - {run.player_name(run.healer_id)} at {clock(t)}", where(t), hp_line(t)]
        if aid in cds.casts:
            prev = max((x for x in cds.casts[aid] if x < t), default=None)
            if prev is None:
                tip.append("First use - ready since the start.")
            else:
                back = prev + cds.cd_ms[aid]
                tip.append(f"Came back at {clock(back)}; pressed {(t - back) / 1000:.0f}s later." if t > back + 1000
                           else "Pressed as soon as it came back.")
        markers.append({"t_s": rel(t), "kind": kind, "label": name, "tip": tip})

    recap_by = {(r["t"], r["victim"]): r for r in recaps}
    for t, victim, ability in run.deaths:
        r = recap_by.get((t, run.player_name(victim)))
        tip = [f"{run.player_name(victim)} died at {clock(t)}", where(t)]
        if r:
            tip.append(r["cause"]["lines"][0] if r["cause"]["lines"] else f"Killed by {run.ability_name(ability)}")
            if r["cause"]["name"] != r["killing"]:
                tip.append(f"Killing blow: {r['killing']}")
            if r.get("avoid"):
                tip.append(f"⚠ {r['avoid']['line']}")
            if r.get("story") and r["story"]["advice"]:
                tip.append("→ " + r["story"]["advice"][0])
        else:
            tip.append(f"Killed by {run.ability_name(ability)}")
        markers.append({"t_s": rel(t), "kind": "death", "label": f"{run.player_name(victim)} died", "tip": tip})

    uncovered = []
    for i, seg in enumerate(segs):
        prof = profiles[i] if i < len(profiles) else None
        for h in seg.hits:
            b = tl.bin_of(h.t)
            after = min(tl.group_ehp[b:b + 3] or [tl.group_ehp[b]])
            up = [s for s in active_at(spans, h.t)]
            # Covered = a group defensive, or personals/externals on at least half the party.
            # (A tank's Barkskin alone doesn't make a group-wide hit "covered".)
            on = {s["target"] for s in up if s["scope"] != "group"}
            covered = any(s["scope"] == "group" for s in up) or len(on) * 2 >= max(1, len(players))
            names: dict[str, set] = {}
            for s in up:
                names.setdefault(s["name"], set()).add(s["target"])
            up_text = ", ".join(f"{n} ({'everyone' if len(ts) >= len(players) else ', '.join(sorted(pname(x) for x in ts))})"
                                for n, ts in sorted(names.items()))
            top_pre = prof.hit_pre.get(h.ability) if prof else None
            tip = [f"{run.ability_name(h.ability)} at {clock(h.t)} - hit {h.targets} players for ~{h.avg_frac:.0%} "
                   f"of max HP each", where(h.t),
                   f"Group {pct(h.pre_ehp)} before → {pct(after)} after"
                   + (f"; back up in {h.recovery_s:.0f}s" if h.recovery_s else "; not back up within 30s"),
                   f"Top groups held {pct(top_pre)} before this hit" if top_pre is not None else "",
                   (("Covered: " if covered else "Not covered - only ") + up_text) if up
                   else "Not covered - no defensive active on anyone"]
            tag = avoid.tag_at(run, h.ability, h.t) if avoid else None
            if tag:
                tip.insert(1, f"⚠ {tag.label} - {tag.evidence}")
            # Highlight only big hits where nobody had any protective defensive up at all.
            bare = h.avg_frac >= UNCOVERED_MIN_HIT and not up
            markers.append({"t_s": rel(h.t), "kind": "hit", "label": run.ability_name(h.ability),
                            "tip": [x for x in tip if x], "covered": not bare, "avoid": tag.label if tag else None})
            if bare:
                uncovered.append(rel(h.t))
    for c in chains:
        markers.append({"t_s": rel(c.t), "kind": "chain", "label": "Chain pull", "risky": c.risky,
                        "tip": [f"{run.pulls[c.prev_idx].name} → {run.pulls[c.idx].name} at {clock(c.t)}"]
                        + _chain_text(c)})
    # Kickable casts that went through: on the big-hits lane, even when they did no group-wide damage.
    for w in (avoid.went_through if avoid else []):
        markers.append({"t_s": rel(w["t"]), "kind": "hit", "label": f"{w['ability']} went through",
                        "avoid": "Went through", "kick": True,
                        "tip": [f"{w['ability']} went through at {clock(w['t'])}", where(w["t"]),
                                f"⚠ {w['tag'].evidence}"]})

    for i, f in enumerate(result.findings):
        if f.pull != "Whole run":
            markers.append({"t_s": rel(f.t), "kind": "finding", "label": f.title, "index": i, "severity": f.severity,
                            "tip": [f.title, f.pull]})
    # ---- full-run overview: Bloodlust, cooldown availability, one line per pull ----
    lust = [{"t_s": rel(c[0]), "label": f"{run.ability_name(c[2])} ({pname(c[1])})"} for c in group_casts
            if run.ability_name(c[2]) in LUST_SPELLS]
    in_combat = [(p.start, p.end) for p in run.pulls]
    ready_lanes = []
    personal = CooldownTracker(run, personals)
    for tracker, aid, name in [(cds, a, n) for a, (n, _) in healing.items() if a in cds.casts] + \
            [(personal, a, n) for a, (n, _) in personals.items()]:
        casts = tracker.casts.get(aid, [])
        cd = tracker.cd_ms.get(aid, tracker.cooldowns[aid][1] * 1000)
        spans_ready, back = [], run.start
        for t in casts + [run.end]:
            if t > back:  # ready from `back` until this cast; keep only the parts inside pulls
                for a, b in in_combat:
                    lo, hi = max(a, back), min(b, t)
                    if hi - lo >= 1000:
                        spans_ready.append([rel(lo), rel(hi)])
            back = t + cd
        merged = []
        for a, b in spans_ready:  # back-to-back pulls: one stretch, not two
            if merged and a - merged[-1][1] <= 1:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        spans_ready = merged
        ready_lanes.append({"name": spec.short_names.get(name, name), "ready": spans_ready,
                            "ready_s": round(sum(b - a for a, b in spans_ready))})
    findings_by_pull: dict[str, list] = {}
    for f in result.findings:
        findings_by_pull.setdefault(f.pull.split(" · ")[0], []).append(f)
    req = {r["pull"]: r for r in result.requirements}
    pull_lines = {}
    for i, p in enumerate(run.pulls):
        label = f"Pull {i + 1}: {p.name}" + (" (boss)" if p.is_boss else "")
        fs = sorted(findings_by_pull.get(label, []), key=lambda f: -f.severity)
        r = req.get(label)
        line = (fs[0].short or fs[0].title) if fs else (
            f"Group HP {r['group_hp']:.0%} vs top {r['top_group_hp']:.0%}" if r and r.get("top_group_hp") else "Nothing flagged")
        # Every death in the pull with the recap's cause - one line can't tell two deaths apart.
        died = [r for r in recaps if p.start <= r["t"] <= p.end]
        avoid_line = ""
        if avoid:
            tags = avoid.by_pull.get(i, {}).values()
            went = [w for w in avoid.went_through if p.start <= w["t"] <= p.end]
            parts = [f"{t.ability} ({sum(t.players.values())} hits)" for t in tags]
            if went:
                parts.append(f"{len(went)} kickable cast{'s' if len(went) > 1 else ''} went through")
            avoid_line = ("Avoidable: " + ", ".join(parts)) if parts else ""
        pull_lines[label] = {"verdict": line, "deaths": sum(1 for d in run.deaths if p.start <= d[0] <= p.end),
                             "death_lines": [f"{r['clock']} {r['victim']}: {r['short'][0].lower() + r['short'][1:]}"
                                             + (f" ({r['avoid']['label'].lower()})" if r.get("avoid") else "")
                                             for r in died],
                             "avoid": avoid_line,
                             "chain": next((_chain_line(run, c) for c in chains if c.idx == i), "")}

    lanes = [{"id": p.id, "name": p.name, "class": p.class_name, "role": p.role}
             for p in sorted(players, key=lambda p: {"tank": 0, "healer": 1, "dps": 2}.get(p.role, 3))]
    defense = [{"lane": s["target"], "name": s["name"], "scope": s["scope"], "start_s": rel(s["start"]),
                "end_s": rel(s["end"]), "protective": s["protective"],
                "tip": [f"{s['name']} on {pname(s['target'])}" + (f" from {pname(s['source'])}" if s["source"] != s["target"] else ""),
                        f"{clock(s['start'])}-{clock(s['end'])} ({(s['end'] - s['start']) / 1000:.0f}s) · {s['scope']}"]}
               for s in spans]
    pulls = []
    for i, (seg, prof) in enumerate(zip(segs, profiles)):
        if seg.duration_s < 10:
            continue
        p = seg.pull
        label = f"Pull {i + 1}: {p.name}" + (" (boss)" if p.is_boss else "")
        pulls.append({"label": label, "boss": p.is_boss, **pull_lines.get(label, {}),
                      "start_s": rel(p.start), "end_s": rel(p.end),
                      "target": round(prof.target_ehp, 3) if prof else None,
                      "floor": round(prof.floor_ehp, 3) if prof else None,
                      "top_curve": _top_curve(seg, tops),
                      "phases": [{"name": ph.name, "start_s": rel(ph.start), "end_s": rel(ph.end)} for ph in phases.get(i, [])]})
    mana = [state.mana_at(tl.start + b * tl.bin_ms) for b in range(len(tl.group_ehp))] if state and state.mana else []
    return {
        "bin_s": tl.bin_ms / 1000,
        "group": [round(v, 3) for v in tl.group_ehp],
        "lowest": [round(v, 3) for v in tl.min_ehp],
        "mana": [None if m is None else round(m, 3) for m in mana],
        "pulls": pulls,
        "markers": sorted(markers, key=lambda m: m["t_s"]),
        "lanes": lanes,
        "defense": defense,
        "uncovered_hits": uncovered,
        "lust": lust,
        "ready_lanes": ready_lanes,
    }
