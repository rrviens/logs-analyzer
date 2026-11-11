"""One full analysis of a run, as a JSON-ready dict. Shared by the CLI and the web app.

Results are cached per run + rules version, so reopening a key is instant. Bump
RULES_VERSION when findings logic changes.
"""
import os
import json
import re
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.baseline import comparable_fights
from app.composition import describe, intake_modifiers, load_effects
from app.context import build_context
from app.context_model import load_dungeon
from app.chains import OUTCOME_S, Chain, find_chains
from app.dps_windows import (BUCKET_LABEL, apply_peer_rates, bucket, calm_bins, calm_dps, damage_model, fetch_damage_done,
                             find_windows, gear_factor, learn_peer_rates, median_rate, summarize)
from app.encounters import Encounter, load_encounter
from app.finding_parts import structure
from app.findings import DEATH_GROUP_MS as SAME_EVENT_MS, WIPE_DEATHS, WIPE_WINDOW_S, Analysis, Finding, TopRun, analyze
from app.metrics import hot_refreshes, hots_active
from app.reports import ability_icons, get_fights, get_players, get_trinkets, parse_report_url, pick_fight
from app.runs import _events, fetch_run
from app.segments import all_segments, build_profile, match_pull, segment_metrics
from app.avoidable import Avoidance, build as build_avoidance, fetch_kicks
from app.auras import dispellable, fetch_auras, learn_self_clears, top_debuff_handling
from app.deaths import (ENVIRONMENTAL, MAX_RECAPS, _applies, ability_schools, clear_tools, fetch_death_events, fetch_group_casts,
                        judge_reaction, learn_dispels, load_defensives, recap_death)
from app.context import CooldownTracker
from app.defense_spans import active_at, fetch_defense_spans
from app.healer_state import fetch_healer_state, fetch_stored
from app.phases import fetch_boss_casts, fetch_boss_hp, phase_breakdown
from app.quality import check as quality_check
from app.spells import fetch_spell_heals, heal_casts
from app.verdicts import _section, items_verdict, phases_verdict, pulls_verdict, spell_verdict
from app.specs import SPECS, healers_in
from app.spellfacts import FACTS
from app.timeline import build_timeline
from app.wcl import WCLClient, WCLError, parallel_map

log = logging.getLogger("uvicorn.error")
RULES_VERSION = 85  # 83: key-level parses in the roster (+ peers' per-pull cooldown use, in progress)
QUALITY_BACKFILL = 6  # extra ranked logs to try when some fail the data-quality gate
COMP_POOL = 4  # extra ranked logs considered so the closest group compositions can be picked
# Top logs fetched in parallel (cold cache: each is several pages of events). More workers = faster but more
# memory at once; set TOP_WORKERS=1 on a small (512 MB) host.
TOP_WORKERS = max(1, int(os.getenv("TOP_WORKERS", "3")))
CURVE_POINTS = 60
LUST_SPELLS = {"Heroism", "Bloodlust", "Time Warp", "Fury of the Aspects", "Primal Rage", "Ancient Hysteria",
               "Netherwinds", "Drums of the Maelstrom"}
UNCOVERED_MIN_HIT = 0.18  # only flag a big hit without your cooldowns when it's this big (share of max HP per player)
UNCOVERED_LOW_EHP = 0.6  # ...and the group dropped below this after it
OWN_CD_BEFORE_MS, OWN_CD_AFTER_MS = 10000, 3000  # your cooldown pressed this close counts as answering the hit
MINOR_CD_S = 60  # defensives on a shorter cooldown are near-rotational: "minor", behind a toggle
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


def _by_comp(cands: list, pub, fight, my_players, effects) -> list:
    """Top-log candidates, closest group composition first: same tank spec, the same damage-reduction specs
    (they change how much damage the group takes), shared specs, then the nearest key level; ranking order breaks ties."""
    label = lambda p: f"{p.spec} {p.class_name}"
    my_tank = {label(p) for p in my_players if p.role == "tank"}
    my_mods, my_specs = intake_modifiers(my_players, effects), {label(p) for p in my_players}

    def score(fp):
        players = get_players(pub, fp[0])
        return (3 * bool(my_tank & {label(p) for p in players if p.role == "tank"})
                - 3 * len(my_mods ^ intake_modifiers(players, effects))
                + len(my_specs & {label(p) for p in players})
                - 2 * abs((fp[0].keystone_level or 0) - (fight.keystone_level or 0)))

    scores = {id(fp): score(fp) for fp in cands}
    return sorted(cands, key=lambda fp: -scores[id(fp)])  # stable: ranking order within equal scores


def cache_key(code: str, fight_id: int, player: str, top: int) -> str:
    return f"analysis:v{RULES_VERSION}:{code}:{fight_id}:{player.lower()}:{top}"


def _preview(client, fight, run, tl, segs, me, spec, players, state, effects) -> dict:
    """Your run alone, before any top log: header, group and the timeline (HP, your cooldowns, deaths, party
    defensives). Top-player bands, findings and comparisons come with the full result."""
    group_casts = fetch_group_casts(client, fight, {p.id for p in players})
    spans = fetch_defense_spans(client, fight, run, players, load_defensives())
    timeline = _timeline(run, tl, segs, [None] * len(segs), {}, state, get_trinkets(client, fight).get(me.id, []),
                         Analysis([], [], []), (), (), players, spans, group_casts, None, (), spec,
                         ability_icons(client, fight.report_code), load_encounter(run.dungeon), get_trinkets(client, fight))
    timeline["dps_windows"] = []
    return {"preview": True,
            "run": {"code": fight.report_code, "fight_id": fight.id, "dungeon": fight.name, "level": fight.keystone_level,
                    "timed": bool(fight.kill), "player": me.name, "duration_min": round(fight.duration_s / 60, 1),
                    "deaths": len(run.deaths)},
            "spec": {"key": spec.key, "label": spec.label, "plural": spec.plural},
            "group": _with_brings(describe(players, effects), players, run, segs, group_casts),
            "findings": [], "timeline": timeline}


SPELL_LINK_SKIP = {"Melee", "Auto Attack", "Shoot", "Unknown"}


def _spell_ids(run, out: dict) -> dict[str, int]:
    """Spell id per ability named anywhere in the result's text - the page turns those names into hover tooltips."""
    text = json.dumps({k: out.get(k) for k in ("findings", "deaths", "verdicts", "phases", "items", "group",
                                               "good_calls", "spell_choice")}, ensure_ascii=False)
    ids: dict[str, int] = {}
    for aid, name in run.abilities:
        if aid > 0 and len(name) >= 4 and name not in SPELL_LINK_SKIP and name not in ids and name in text:
            ids[name] = aid
    return ids


STOP_SHARE = 0.25  # a stoppable cast must have done at least this share of the damage in the last 10s
STOP_WINDOW_MS = 12000  # ...and completed this close before the death


def _stoppable(r: dict, avoid: Avoidance, run) -> dict | None:
    """A death mostly caused by an enemy cast peer groups usually stop (interrupt, stun or CC) - and who in the group
    had something ready to stop it (the healer first)."""
    hits: dict[str, float] = {}
    for e in r.get("events", []):
        if e.get("kind") == "hit" and e.get("ability"):
            hits[e["ability"]] = hits.get(e["ability"], 0.0) + (e.get("amount") or 0)
    total = sum(hits.values())
    best = None
    for w in avoid.went_through:
        share = hits.get(w["ability"], 0.0) / total if total else 0.0
        if r["t"] - STOP_WINDOW_MS <= w["t"] <= r["t"] and share >= STOP_SHARE and (not best or share > best[1]):
            best = (w, share)
    if not best:
        return None
    w, share = best
    healer = run.player_name(run.healer_id)
    kickable = FACTS.interruptible(w.get("ability_id", 0)) is not False  # game data: some casts only stop to a stun
    ready = {}
    for who, names in list(w.get("ready_kicks", {}).items() if kickable else []) + list(w.get("ready_cc", {}).items()):
        ready.setdefault(who, []).extend(n for n in names if n not in ready.get(who, []))
    order = sorted(ready, key=lambda who: (who != healer, who))  # your own tools first
    return {"ability": w["ability"], "share": round(share, 2), "rate": round(w.get("rate", 0), 2), "seen": w.get("seen"),
            "kickable": kickable, "ready": [{"player": who, "you": who == healer, "tools": ready[who]} for who in order]}


KICK_PULL_MIN = 3  # this many kickable damage casts going through on one pull is worth a finding
KICK_HIT_MS = 3000  # the cast counts when its damage lands on a player this soon after it completes


def _kick_findings(run, avoid: Avoidance, clock) -> list[Finding]:
    """Pulls where several damage casts went through that peers interrupt (70%+ of the time - so casts a tank
    reflects or soaks on purpose never count, and the notes' no-kick casts are dropped earlier) and the game data says can be
    interrupted and have a cast time. One finding per pull, each cast type with how often peers stop it."""
    out = []
    for i, p in enumerate(run.pulls):
        went = [w for w in avoid.went_through if p.start <= w["t"] <= p.end
                and FACTS.interruptible(w["ability_id"]) and FACTS.cast_ms(w["ability_id"])
                and any(d[2] == w["ability_id"] and w["t"] <= d[0] <= w["t"] + KICK_HIT_MS for d in run.damage)]
        if len(went) < KICK_PULL_MIN:
            continue
        kinds: dict[str, list[dict]] = {}
        for w in went:
            kinds.setdefault(w["ability"], []).append(w)
        lines = [f"{name} x{len(ws)} - peer groups stop it {ws[0]['rate']:.0%} of the time; "
                 f"{sum(1 for w in ws if w['ready'])} of these with an interrupt ready"
                 for name, ws in sorted(kinds.items(), key=lambda x: -len(x[1]))]
        ready = sum(1 for w in went if w["ready"])
        out.append(Finding(went[0]["t"], _pull_label(run, i), min(70, 40 + 5 * len(went)),
                           f"{len(went)} kickable damage casts went through on {p.name}", lines,
                           [["Interrupts", f"{ready} of {len(went)} went through while someone had an interrupt ready."]],
                           rule="kicks", short=f"{len(went)} kickable casts went through", fix="Assign kicks"))
    return out


def _stoppable_finding(recaps: list[dict], clock) -> Finding | None:
    """Two or more deaths after casts the group could have stopped: one finding, each death as evidence."""
    hits = [r for r in recaps if r.get("stoppable")]
    if len(hits) < 2:
        return None
    lines = []
    for r in hits:
        s = r["stoppable"]
        who = "; ".join(f"{'you' if x['you'] else x['player']}: {', '.join(x['tools'])}" for x in s["ready"][:4]) or "nothing was ready"
        lines.append(f"{clock(r['t'])} {r['victim']} - {s['ability']} did {s['share']:.0%} of the damage; peer groups stop it "
                     f"{s['rate']:.0%} of the time. Ready: {who}.")
    return Finding(hits[0]["t"], hits[0].get("pull") or "Whole run", 55,
                   f"{len(hits)} deaths after casts your group could have stopped", lines,
                   [["Peers", "Peer groups interrupt, stun or CC these casts most of the time."]], rule="stoppable",
                   short=f"{len(hits)} deaths after stoppable casts",
                   fix="Stun / interrupt that cast")


METERS_QUERY = """query($code: String!, $f: [Int]) { reportData { report(code: $code) {
  damage: table(fightIDs: $f, dataType: DamageDone) healing: table(fightIDs: $f, dataType: Healing) } } }"""


# This share of your healing on a pull went to a non-player: a heal check. Normal pulls (pets, totems) stay under 7%
# across 6 dungeons; Avatar of Sethraliss is 32-51% at +22 (checked Nov 2026).
NPC_HEAL_SHARE = 0.1


def _heal_check_pulls(client, fight, run) -> set[int]:
    """Pulls where a real share of your healing went to a friendly NPC (heal the Avatar...) - the pressure there isn't
    on the group's HP, so healing cooldowns pressed then aren't "no pressure". One cached request per key."""
    key = f"npcheal:v1:{fight.report_code}:{fight.id}:{run.healer_id}"
    rows = client.get_cached(key)
    if rows is None:
        rows = [[e["timestamp"], e.get("targetID"), e.get("amount", 0)]
                for e in _events(client, fight, "Healing", source_id=run.healer_id) if e.get("type") == "heal"]
        client.set_cached(key, rows)
    marked = load_encounter(run.dungeon).heal_checks  # by hand, so a boss healed very little still counts
    out = {i for i, p in enumerate(run.pulls) if p.name in marked}
    for i, p in enumerate(run.pulls):
        inside = [(tgt, amt) for t, tgt, amt in rows if p.start <= t <= p.end]
        total = sum(a for _, a in inside)
        if total and sum(a for tgt, a in inside if tgt not in run.player_ids) / total >= NPC_HEAL_SHARE:
            out.add(i)
    return out


def _meters(client, fight) -> dict | None:
    """Damage and healing per player over the whole key (one cached request) - the group's meters."""
    try:
        r = client.query(METERS_QUERY, {"code": fight.report_code, "f": [fight.id]})["reportData"]["report"]
    except (WCLError, KeyError, TypeError):
        return None  # meters are a nice-to-have: never fail the analysis over them
    out = {}
    for kind in ("damage", "healing"):
        d = r[kind]["data"]
        secs = max(1, d["totalTime"] / 1000)
        out[kind] = sorted(({"name": e["name"], "class": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", e["type"]),"spec": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", e.get("icon", "").split("-")[-1]),
                             "total": e["total"], "per_s": round(e["total"] / secs)} for e in d["entries"]),
                           key=lambda e: -e["total"])
    return out


PARSES_QUERY = """query($code: String!, $f: [Int]) { reportData { report(code: $code) {
  dps: rankings(fightIDs: $f, playerMetric: dps) hps: rankings(fightIDs: $f, playerMetric: hps) } } }"""


def _with_parses(client, fight, group: list[dict]) -> list[dict]:
    """Each player's key-level DPS and HPS percentiles (WCL's bracketPercent: vs keys of the same level). WCL's
    rankPercent ranks the key's score, the same for all five, so it isn't used. Timed keys only."""
    try:
        r = client.query(PARSES_QUERY, {"code": fight.report_code, "f": [fight.id]})["reportData"]["report"]
        pct: dict[str, dict] = {}
        for metric in ("dps", "hps"):
            for rank in r[metric]["data"][:1]:
                for role in rank["roles"].values():
                    for ch in role.get("characters", []):
                        if ch.get("bracketPercent") is not None:
                            pct.setdefault(ch["name"], {})[metric] = ch["bracketPercent"]
    except (WCLError, KeyError, TypeError, AttributeError):
        return group  # parses are a nice-to-have: never fail the analysis over them
    return [{**m, "parses": pct[m["name"]]} if m["name"] in pct else m for m in group]


def _own_run_sections(client, fight, run, tl, segs, players, state, spec) -> dict:
    def clock(t: int) -> str:
        s = max(0, (t - run.start) // 1000)
        return f"{s // 60}:{s % 60:02d}"
    deaths = _death_recaps(client, fight, run, players, state, [], [], clock, lambda msg: None, spec)
    encounter = load_encounter(run.dungeon)
    for r in deaths:
        r["note"] = encounter.note(name=r["cause"]["name"])
        _judge(r, encounter)
    dps, windows = _dps_windows(client, fight, run, tl, segs, [None] * len(segs), [], [], spec)
    return {"deaths": deaths, "dps": dps, "dps_windows": windows, "meters": _meters(client, fight)}


def latest_key(code: str, fight_id: int, player: str, top: int) -> str:
    return f"analysis_latest:{code}:{fight_id}:{player.lower()}:{top}"


def stored_analysis(client: WCLClient, code: str, fight_id: int, player: str, top: int) -> dict | None:
    """The newest stored analysis of a run (any rules version) - no Warcraft Logs calls."""
    key = client.get_cached(latest_key(code, fight_id, player, top))
    return client.get_cached(key) if key else None


def run_analysis(client: WCLClient, url: str, player_name: str | None = None, top: int = 8,
                 progress: Callable[[str], None] = lambda msg: None, use_cache: bool = True,
                 preview: Callable[[dict], None] | None = None) -> dict:
    """preview: called once with your run alone (header, group, timeline) while the top logs still download."""
    t0, report = time.time(), progress
    progress = lambda msg: report(f"[{time.time() - t0:4.0f}s] {msg}")  # elapsed time shows which step is slow
    code, fight_id = parse_report_url(url)
    fight = pick_fight(get_fights(client, code), fight_id)
    me, spec = pick_healer(get_players(client, fight), player_name)

    key = cache_key(code, fight.id, me.name, top)
    if use_cache:
        cached = client.get_cached(key)
        if cached:
            if not cached.get("key"):  # stored before the "latest" pointers existed
                cached["key"], cached["rules_version"] = key, RULES_VERSION
                client.set_cached(key, cached)
                client.set_cached(latest_key(code, fight.id, me.name, top), key)
                if player_name and player_name.lower() != me.name.lower():
                    client.set_cached(latest_key(code, fight.id, player_name, top), key)
            return cached

    progress(f"Fetching your run ({fight.name} +{fight.keystone_level or 0}) and finding comparable top logs...")
    effects = load_effects()
    my_players = get_players(client, fight)
    my_mods = frozenset(intake_modifiers(my_players, effects))
    pub = client.public()  # ranked top logs are public: fetched once into the shared cache, for every user
    # The top-log search only needs the fight and your group, so it runs while your run downloads.
    finder = ThreadPoolExecutor(1)
    found = finder.submit(lambda: _by_comp(comparable_fights(pub, fight, spec.class_name, spec.spec_name,
                                                             top + QUALITY_BACKFILL + COMP_POOL),
                                           pub, fight, my_players, effects))
    finder.shutdown(wait=False)
    run = fetch_run(client, fight, me.id, spec)
    tl = build_timeline(run)
    segs = all_segments(run, tl, spec)

    my_state = fetch_healer_state(client, fight, run, spec)
    my_state.stored = fetch_stored(client, fight, run, spec)
    my_boss_casts, my_boss_hp = fetch_boss_casts(client, fight, run), fetch_boss_hp(client, fight, run)

    tops, top_boss, candidates, skipped = [], [], [], []
    fetched = [0]

    def load_top(fp):
        """One top log, fully loaded - or (None, reasons) when it fails the data-quality gate."""
        f, p = fp
        other = fetch_run(pub, f, p.id, spec)
        otl = build_timeline(other)
        ok, reasons = quality_check(other.damage, other.player_ids, f.duration_s, fight.duration_s, otl.group_ehp)
        loaded = None
        if ok:
            state = fetch_healer_state(pub, f, other, spec)
            loaded = (TopRun(other, get_trinkets(pub, f).get(p.id, []),
                             frozenset(intake_modifiers(get_players(pub, f), effects)),
                             heal_casts(other, otl, fetch_spell_heals(pub, f, other, spec), state, spec)),
                      otl, fetch_boss_casts(pub, f, other), fetch_boss_hp(pub, f, other))
        fetched[0] += 1
        progress(f"Fetched top log {fetched[0]} ({p.name} +{f.keystone_level}{'' if ok else ', incomplete data - skipped'})")
        return loaded, reasons

    # Extra candidates so logs failing the data-quality gate can be replaced from further down. Fetched
    # TOP_WORKERS at a time, in batches of exactly what is still missing, so no log is fetched for nothing.
    pending = found.result()
    progress(f"Fetching {min(top, len(pending))} top logs, {TOP_WORKERS} at a time...")

    def load_tops():
        nonlocal pending
        with ThreadPoolExecutor(TOP_WORKERS) as pool:
            while pending and len(tops) < top:
                batch, pending = pending[:top - len(tops)], pending[top - len(tops):]
                for (f, p), (loaded, reasons) in zip(batch, pool.map(load_top, batch)):  # keeps ranking order
                    if not loaded:
                        skipped.append({"player": p.name, "key": f.keystone_level, "reasons": reasons})
                        continue
                    candidates.append((f, p))
                    tops.append(loaded[:2])
                    top_boss.append((loaded[0].run, loaded[1], loaded[2], loaded[3]))

    # The top logs download in the background; meanwhile your own run goes to the page as a preview.
    loader = ThreadPoolExecutor(1)
    tops_done = loader.submit(load_tops)
    loader.shutdown(wait=False)
    if preview:
        try:
            part = _preview(client, fight, run, tl, segs, me, spec, my_players, my_state, effects)
            preview(part)
            # Stage 2, still without peers: death recaps and DPS windows only need your own run. (Peer extras -
            # how peers handle a debuff, their DPS rate, avoidable tags - come with the full result.)
            preview({**part, "stage": 2, **_own_run_sections(client, fight, run, tl, segs, my_players, my_state, spec)})
        except Exception:  # noqa: BLE001 - a preview is a nice-to-have; the full analysis still follows
            log.warning("preview failed for %s fight %s", code, fight.id, exc_info=True)
    tops_done.result()

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
    phases, phase_rows = phase_breakdown(run, tl, my_boss_casts, my_boss_hp, top_boss, spec)

    def clock(t: int) -> str:
        s = max(0, (t - run.start) // 1000)
        return f"{s // 60}:{s % 60:02d}"

    group_casts = fetch_group_casts(client, fight, {p.id for p in my_players})
    progress("Tagging avoidable damage...")
    avoid = build_avoidance(client, fight, run, [(f, tr.run) for (tr, _), (f, _) in zip(tops, candidates)],
                            group_casts)
    # Encounter notes correct the learned tags: an ability the notes call not avoidable loses its tag.
    encounter = load_encounter(run.dungeon)
    # The game data too: "avoidable" (peers took it less) only stands for damage on the floor or a frontal - peers
    # also take less of a cast they interrupt (Frostbolt) or an instant they skip the pack for.
    on_floor = lambda a: not FACTS.ready() or (FACTS.interruptible(a) is not True and FACTS.ground(a) is not None)
    avoid.by_pull = {i: {a: t for a, t in tags.items() if not encounter.not_avoidable(a, run.ability_name(a))
                         and (encounter.avoidable(a, run.ability_name(a)) or on_floor(a))}
                     for i, tags in avoid.by_pull.items()}
    # Casts groups let through on purpose (a tank reflects Fiery Blast) are never "went through".
    avoid.went_through = [w for w in avoid.went_through if not encounter.no_kick(w["ability_id"], w["ability"])]
    chains = find_chains(run, tl, fetch_auras(client, fight, run.player_ids), my_state, tops, group_casts, spec)
    death_recaps = _death_recaps(client, fight, run, my_players, my_state, [tr.run for tr, _ in tops],
                                 candidates, clock, progress, spec)
    for r in death_recaps:
        tag = avoid.tag_by_name(run, r["cause"]["name"], r["t"])
        r["avoid"] = {"label": tag.label, "line": tag.line()} if tag else None
        r["note"] = encounter.note(name=r["cause"]["name"])
        r["stoppable"] = _stoppable(r, avoid, run)
        r["dodge"] = _dodgeable(r, avoid, encounter, run, [tr.run for tr, _ in tops])
        r["pull_melee"] = _pull_melee(r, run, chains)
        r["skip"] = _failed_skip(r, run, group_casts)
        if r["dodge"] or r["pull_melee"] or r["skip"]:  # no "would have lived" on the card for damage you walk out of
            for i in (i for items in r["ready"].values() for i in items):
                i.pop("verdict", None)
        _judge(r, encounter)
    spans = fetch_defense_spans(client, fight, run, my_players, load_defensives())
    group_defs, seen = [], []
    for s in sorted(spans, key=lambda s: s["start"]):  # one entry per press (a group span is logged per player)
        if s["scope"] == "group" and s["source"] != run.healer_id and s["source"] in run.player_ids and \
                not any(n == s["name"] and abs(t - s["start"]) <= 2000 for t, n in seen):
            seen.append((s["start"], s["name"]))
            group_defs.append((s["start"], s["name"], run.player_name(s["source"])))
    result = analyze(run, tl, segs, profiles, ctx, model, [tr for tr, _ in tops], my_trinkets, my_mods,
                     my_heals, phases, phase_rows, my_state, death_recaps, spec=spec, group_defs=group_defs,
                     went_through=avoid.went_through, heal_checks=_heal_check_pulls(client, fight, run))
    result.findings += _chain_findings(run, chains, clock)
    result.findings += _kick_findings(run, avoid, clock)
    if (stop := _stoppable_finding(death_recaps, clock)):
        result.findings.append(stop)
    _soften(result.findings, run, avoid, death_recaps, clock)
    comp_notes = [
        f"{mod}: in your group; {sum(1 for tr, _ in tops if mod in tr.intake_modifiers)} of {len(tops)} "
        f"peer groups had one too." for mod in sorted(my_mods)
    ]

    def as_dict(f):
        return {"clock": clock(f.t), "t_s": round((f.t - run.start) / 1000, 1), "pull": f.pull,
                "severity": f.severity, "title": f.title, "detail": f.detail, "context": f.context, "rule": f.rule,
                "short": f.short, "fix": f.fix, "parts": structure(f.detail, f.context)}

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
        "encounter": {"covered": encounter.covered},
        "compared": {
            "count": len(tops),
            "levels": sorted({tr.run.keystone_level for tr, _ in tops}),
            "model": model.runs_label() if model else None,
            "skipped": skipped,
            "logs": [{"code": f.report_code, "fight_id": f.id, "player": p.name, "key": f.keystone_level,
                      "timed": bool(f.kill), "duration_min": round(f.duration_s / 60, 1)} for f, p in candidates],
        },
        "group": _with_parses(client, fight, _with_brings(describe(my_players, effects), my_players, run, segs,
                                                          fetch_group_casts(client, fight, {p.id for p in my_players}))),
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
        "meters": _meters(client, fight),
        "timeline": _timeline(run, tl, segs, profiles, phases, my_state, my_trinkets, result, tops, death_recaps,
                              my_players, spans,
                              group_casts, avoid, chains, spec, ability_icons(client, code), encounter,
                              get_trinkets(client, fight),
                              [model.band_segment(s.pull) if model else (None, False) for s in segs]),
    }
    progress("Finding DPS windows...")
    out["dps"], out["timeline"]["dps_windows"] = _dps_windows(client, fight, run, tl, segs, profiles, tops,
                                                               candidates, spec)
    out["spell_ids"] = _spell_ids(run, out)
    out["key"], out["rules_version"] = key, RULES_VERSION
    client.set_cached(key, out)
    # The newest stored analysis of this run, whatever the rules version: shown at once (marked as updating)
    # when the rules change, instead of making you wait for - or depend on - Warcraft Logs.
    client.set_cached(latest_key(code, fight.id, me.name, top), key)
    if fight_id is not None and player_name and player_name.lower() != me.name.lower():
        client.set_cached(latest_key(code, fight.id, player_name, top), key)  # looked up by the name you asked for
    return out


def _death_recaps(client, fight, run, players, state, top_runs, candidates, clock, progress, spec) -> list[dict]:
    """Recap for each death: cause, what was ready (group / victim / healer), the debuff and
    dispel story, the victim's last casts, and how top players handle the same debuff."""
    deaths = [d for d in sorted(run.deaths, key=lambda d: d[0])
              if d[2] or d[0] < run.end - 10_000]  # skip end-of-key resets
    after = _after_tank(run, deaths)
    deaths = [d for d in deaths if (d[0], d[1]) not in after][:MAX_RECAPS]
    if not deaths:
        return []
    progress("Building death recaps...")
    defensives = load_defensives()
    tools = clear_tools(defensives)
    group_casts = fetch_group_casts(client, fight, {p.id for p in players})
    schools = ability_schools(client, fight)
    auras = fetch_auras(client, fight, {p.id for p in players})
    pub = client.public()
    top_auras = list(zip(top_runs, parallel_map(lambda trf: fetch_auras(pub, trf[1][0], trf[0].player_ids),
                                                list(zip(top_runs, candidates)))))
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
    own_dispel, totems = learn_dispels([(auras, run.healer_id, run.ability_name)]
                                       + [(a, tr.healer_id, tr.ability_name) for tr, a in top_auras])
    # The game's own data says each debuff's type: that decides what your dispel removes (logs only fill in
    # this season's newest spells, which the data may not have yet).
    for aid in {d[1] for d in auras.debuffs}:
        kind = FACTS.dispel(aid)
        if kind:
            can_dispel.add(aid)
            (own_dispel.add if kind in spec.dispels else own_dispel.discard)(aid)
    out = []
    events = parallel_map(lambda d: fetch_death_events(client, fight, d[1], d[0]), deaths)
    for (t, victim, killing), ev in zip(deaths, events):
        r = recap_death(run, players, group_casts, defensives, ev, t, victim, killing, schools,
                        auras, can_dispel, self_clears, handling, state.procs() if state else None,
                        own_dispel, totems, _stored_items(state, spec))
        r["clock"] = clock(t)
        r["pull"] = next((f"Pull {i + 1}: {p.name}" for i, p in enumerate(run.pulls) if p.start <= t <= p.end), "")
        r["short"], r["fix"] = _death_short(r)
        r["after_tank"] = [{"clock": clock(at), "victim": run.player_name(av)}
                           for (at, av), tank_t in sorted(after.items()) if tank_t == t]
        out.append(r)
    return out


def _stored_items(state, spec) -> list[tuple]:
    """(name, spans, effect, duration) for each stored heal, for the death recaps."""
    if not state:
        return []
    return [(n, state.stored.get(n, []), eff, dur) for n, eff, dur in spec.stored_cooldowns.values()]


def _after_tank(run, deaths: list[list]) -> dict[tuple[int, int], int]:
    """Deaths in a wipe that came after the tank died: (t, victim) -> the tank's death time. Once the tank is down
    the pull is lost, so these get no recap of their own - they're listed on the tank's card."""
    out = {}
    pull_of = lambda t: next((i for i, p in enumerate(run.pulls) if p.start <= t <= p.end), None)
    for tank_t, tank, _ in deaths:
        if tank not in run.tank_ids:
            continue
        p = pull_of(tank_t)
        near = [d for d in deaths if pull_of(d[0]) == p and abs(d[0] - tank_t) <= WIPE_WINDOW_S * 1000]
        if len(near) < WIPE_DEATHS:
            continue  # the tank died but the group held: every death still counts
        for t, victim, _ in near:
            # Dying in the same moment as the tank (same hit) still counts; only the deaths that followed don't.
            if t - tank_t > SAME_EVENT_MS and victim not in run.tank_ids and (t, victim) not in out:
                out[(t, victim)] = tank_t
    return out


def _dps_windows(client, fight, run, tl, segs, profiles, tops, candidates, spec) -> tuple[dict, list[dict]]:
    """Calm windows in your run (healthy group by that pull's top-group standard, no big hit coming) and the damage
    you dealt in them, vs top healers' damage per second in their own calm moments."""
    calm = calm_bins(run, tl, [h.t for s in segs for h in s.hits],
                     {i: p.target_ehp for i, p in enumerate(profiles) if p})
    damage = fetch_damage_done(client, fight, run)
    model = damage_model(run, damage, spec)  # fallback when peers have no calm data for this many enemies
    windows = find_windows(run, tl, calm, damage, model, fetch_kicks(client, fight, run.player_ids)["casts"])
    rates, pub, peer_data = [], client.public(), []
    for (tr, otl), (f, _) in zip(tops, candidates):
        top_segs = all_segments(tr.run, otl, spec)
        top_calm = calm_bins(tr.run, otl, [h.t for s in top_segs for h in s.hits])
        top_damage = fetch_damage_done(pub, f, tr.run)
        top_windows = find_windows(tr.run, otl, top_calm, top_damage, None,
                                   fetch_kicks(pub, f, tr.run.player_ids)["casts"])
        rates.append(calm_dps(top_windows))
        peer_data.append((tr.run, top_windows, top_damage))
    top_rate = median_rate(rates)
    if peer_data:  # what peers really deal with this many enemies (DoTs, cooldowns, target caps included)
        peers = learn_peer_rates(peer_data)
        apply_peer_rates(windows, peers, gear_factor(run, damage, peers))
    rel = lambda t: round((t - run.start) / 1000, 1)
    rows = []
    for w in windows:
        mine = w.damage / w.seconds if w.seconds else 0
        if w.potential is not None:  # vs what you could have cast here
            use = min(1.0, w.damage / w.potential) if w.potential else 1.0
            missed = w.missed
        else:  # no spell model: vs top healers' calm-time rate
            use = min(1.0, mine / top_rate) if top_rate else None
            missed = max(0.0, top_rate * w.seconds - w.damage) if top_rate else None
        enemies = f"{w.enemies} enem{'ies' if w.enemies != 1 else 'y'} casting nearby"
        if w.potential and w.mix:
            could = (f"Could have dealt ~{w.potential / 1e6:.1f}M: peer {spec.plural} with {BUCKET_LABEL[bucket(w.enemies)]} "
                     f"deal that at your gear, mostly " + ", ".join(f"{n} {s:.0%}" for n, s in w.mix) + f" ({enemies})")
        elif w.potential:
            could = f"Could have dealt ~{w.potential / 1e6:.1f}M with your own {_could_spell(spec, w.enemies)} ({enemies})"
        else:
            could = ""
        s = max(0, (w.start - run.start) // 1000)
        rows.append({"start_s": rel(w.start), "end_s": rel(w.end), "use": round(use, 2) if use is not None else None,
                     "missed": round(missed or 0), "clock": f"{s // 60}:{s % 60:02d}", "seconds": round(w.seconds),
                     "pull": next((_pull_label(run, i) for i, p in enumerate(run.pulls)  # 1s bins can start just before
                                   if p.start - tl.bin_ms <= w.start <= p.end), ""),
                     "dps_casts": w.dps_casts, "other_casts": w.other_casts, "idle_s": round(w.idle_s),
                     "tip": [f"Safe DPS window: {w.seconds:.0f}s (no damage coming in, group healthy or being "
                             f"healed, no big hit coming)", could,
                             f"You: {w.dps_casts} damage cast{'s' if w.dps_casts != 1 else ''}, {w.other_casts} other "
                             f"(heals into a healthy group, buffs), ~{w.idle_s:.0f}s idle",
                             f"Your damage here: {mine / 1000:.0f}k/s" + (f" vs ~{top_rate / 1000:.0f}k/s for peer "
                                                                        f"{spec.plural} in their calm moments" if top_rate else ""),
                             f"~{missed / 1e6:.1f}M damage left on the table" if missed and missed >= 1e5 else
                             ("Used it about as well as your peers" if top_rate else ""),
                             "Idle time can also mean moving or out of range" if w.idle_s >= 3 else ""]})
    return summarize(windows, top_rate), rows


def _could_spell(spec, enemies: int | None) -> str:
    return spec.dps_aoe if (enemies or 0) >= 2 and spec.dps_aoe else " / ".join(spec.dps_st) or "damage spells"


def _pull_label(run, i: int) -> str:
    p = run.pulls[i]
    return f"Pull {i + 1}: {p.name}" + (" (boss)" if p.is_boss else "")


def _chain_text(c: Chain) -> list[str]:
    """Hover lines for a chain pull."""
    lines = [f"Chain pull: {c.spill[0]} still hitting" if c.spill else "Chain pull: no break after the last pack"]
    lines += [f"⚠ {r}" for r in c.risks] if c.risky else (["Nothing risky going in"] + c.risks)
    if c.top_chained:
        lines.append(f"Peer groups chained here in {c.top_chained[0]} of {c.top_chained[1]} runs")
    lines.append(f"Next {OUTCOME_S}s: {c.deaths} death{'s' if c.deaths != 1 else ''}, lowest player {c.min_lowest:.0%}, "
                 f"group down to {c.min_group:.0%}")
    return lines


def _chain_line(run, c: Chain) -> str:
    """One line per chain: 'Pull 7 → 8 · chained · nothing risky · top groups chain here 5/8'."""
    parts = [f"Pull {c.prev_idx + 1} → {c.idx + 1} ({run.pulls[c.idx].name})", "chained",
             "; ".join(c.risks) if c.risky else "nothing risky going in"]
    if c.top_chained:
        parts.append(f"peer groups chain here {c.top_chained[0]}/{c.top_chained[1]}")
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
            context.append(["Peers", f"chained here in {c.top_chained[0]} of {c.top_chained[1]} runs"
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


PULL_MELEE_MS = 15000  # mob melee this soon after a pack comes in: the tank hasn't got it yet
MELEE = {"Melee", "Auto Attack"}


def _pull_melee(r: dict, run, chains) -> str:
    """Why a DPS dying to mob melee is their own threat/positioning, not something to heal through - "" otherwise.
    As a pack comes in (a fresh pull or a chain) the mobs hit whoever they target first, and one-shot a DPS before any
    healer can react."""
    if r["cause"]["name"] not in MELEE or not any(p[1] == r["victim"] and p[2] == "dps" for p in run.players):
        return ""
    pull = next((p for p in run.pulls if p.start <= r["t"] <= p.end), None)
    if pull and r["t"] - pull.start <= PULL_MELEE_MS:
        return f"{(r['t'] - pull.start) / 1000:.0f}s into the pull"
    if any(0 <= r["t"] - c.t <= PULL_MELEE_MS for c in chains):
        return "while packs were chained"
    return ""


SKIP_TOOLS = {"Shadowmeld", "Stealth", "Prowl", "Vanish", "Shroud of Concealment", "Invisibility", "Greater Invisibility",
              "Mass Invisibility", "Camouflage", "Feign Death"}
SKIP_LOOKBACK_MS, SKIP_ALONE_MS, SKIP_END_MS = 30_000, 10_000, 10_000


def _failed_skip(r: dict, run, group_casts) -> str:
    """A skip that went wrong: the victim died alone (nobody else hit in that pull), the "pull" ended right after,
    and the group was using skip tools (meld, stealth, invisibility). A tank gathering mobs for a funnel doesn't match:
    the group then fights those mobs, so others get hit and the pull goes on. Returns the tools seen, or ""."""
    t, vid = r["t"], next((p[0] for p in run.players if p[1] == r["victim"]), None)
    pull = next((p for p in run.pulls if p.start <= t <= p.end), None)
    if vid is None or not pull or pull.end - t > SKIP_END_MS:
        return ""
    others_hit = any(d[1] != vid for d in run.damage if pull.start <= d[0] <= pull.end)
    if others_hit or not any(d[1] == vid for d in run.damage if t - SKIP_ALONE_MS <= d[0] <= t):
        return ""
    tools = sorted({f"{run.player_name(c[1])}: {run.ability_name(c[2])}" for c in group_casts
                    if t - SKIP_LOOKBACK_MS <= c[0] <= t and run.ability_name(c[2]) in SKIP_TOOLS})
    return ", ".join(tools)


DODGE_MIN_TOPS = 3
DODGE_ONE_SHOT = 0.5  # a killing blow this big (share of max HP) counts as the cause on its own
DODGE_PEER_MAX = 1.0  # a ground effect peers still take this often per player a run isn't really avoidable


def _dodgeable(r: dict, avoid: Avoidance, encounter, run, top_runs: list) -> dict | None:
    """{ability, why} when the death's cause (or killing blow) was damage to dodge, not to survive. A death to
    ground effects or things to walk out of gets no defensive advice: standing in it was the mistake."""
    # The killing blow only decides it when it was a big hit on its own (a one-shot), not a tick that finished them.
    last = next((e for e in reversed(r.get("events", [])) if e["kind"] == "hit" and e["ability"] == r.get("killing")), None)
    big_kill = bool(last and r.get("max_hp") and (last.get("amount") or 0) >= DODGE_ONE_SHOT * r["max_hp"])
    for name in dict.fromkeys([r["cause"]["name"]] + ([r["killing"]] if big_kill else [])):
        if not name:
            continue
        aid = next((a for a, n in run.abilities if n == name), None)
        if encounter.avoidable(aid, name):
            return {"ability": name, "why": encounter.note(aid, name) or "a ground effect to dodge"}
        # Only the game data can say "dodge": peers being hit less also happens when they interrupt the cast (Frostbolt),
        # stun the caster or skip the pack - so peer numbers can veto, never confirm.
        ids = [a for a, n in run.abilities if n == name]
        if any(FACTS.interruptible(a) for a in ids):
            continue  # a cast you interrupt, not something on the floor
        kinds = {FACTS.ground(a) for a in ids} - {None}
        victim_tank = any(p[1] == r["victim"] and p[2] == "tank" for p in run.players)
        if not ("ground" in kinds or ("cone" in kinds and not victim_tank)):
            continue
        hits = [sum(1 for d in t.damage if d[2] in {a for a, n in t.abilities if n == name}) / max(1, len(t.players))
                for t in top_runs]
        peer_pp = statistics.mean(hits) if len(top_runs) >= DODGE_MIN_TOPS else None
        if peer_pp is not None and peer_pp > DODGE_PEER_MAX:
            continue  # on the floor, but peers get hit by it all the time: not really avoidable
        where = "lands on a spot on the ground" if "ground" in kinds else "is a frontal"
        return {"ability": name, "why": f"it {where} (game data)"
                + (f"; peer groups get hit {peer_pp:.1f} times per player a run" if peer_pp is not None else "")}
    return None


KNOWN_KINDS = {"group", "tank", "interrupt"}  # encounter kinds that are predictable (a targeted DoT is not)


def _judge(r: dict, encounter) -> None:
    """Reaction window for the victim's personals: telegraphed (a cast peers stop, so it has a cast time) or a
    known encounter mechanic counts; a random targeted DoT doesn't."""
    e = encounter.entry(r["cause"]["ability"], r["cause"]["name"])
    kinds = set((e or {}).get("kinds", []))
    known = kinds & KNOWN_KINDS or (kinds and "dot" not in kinds and "targeted" in kinds)
    telegraphed = (f"{r['stoppable']['ability']} has a cast time" if r.get("stoppable")
                   else f"{r['cause']['name']} is a known mechanic" if known else "")
    judge_reaction(r, telegraphed)
    r["short"], r["fix"] = _death_short(r)


def _death_short(r: dict) -> tuple[str, str]:
    """Collapsed death card: 'Died to Bloodthorn Roots' (same words as its finding; how long a debuff was on them is
    in the story) and one fix."""
    story, cause = r.get("story"), r["cause"]["name"]
    short = f"Died to {cause}"
    every = [i for i in r["ready"]["victim"]  # only the victim's own tools are a fix
             if not i.get("escape") and not i.get("no_window") and not i.get("gcd")]
    savers = [i for i in every if i["status"] == "ready" and i.get("verdict") == "lived"]
    if r.get("skip"):
        fix = "Skip went wrong - not healing"
    elif r.get("pull_melee"):  # the DPS's own threat / positioning
        fix = "Let the tank get threat first"
    elif r.get("dodge"):  # standing in it was the mistake, not a missing defensive
        fix = f"Dodge {r['dodge']['ability']}"
    elif story and story.get("fix"):
        fix = story["fix"]
    elif savers and cause not in ENVIRONMENTAL:
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
              spans=(), group_casts=(), avoid: Avoidance | None = None, chains=(), spec=None,
              icons=({}, {}), encounter: Encounter | None = None, all_trinkets: dict | None = None,
              study_curves: list | None = None) -> dict:
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
        return f"Pull {i + 1}: {run.pulls[i].name}" + (f" · after {ph}" if ph and ph != "Start" else "")

    def hp_line(t: int) -> str:
        b = tl.bin_of(t)
        return f"Group {pct(tl.group_ehp[b])} · lowest {pct(tl.min_ehp[b])}"

    healing, personals = spec.healing_cooldowns, spec.personal_cooldowns
    all_trinkets = all_trinkets or {}
    cds = CooldownTracker(run, healing)
    markers = []
    for t, aid, target in run.casts:
        name = run.ability_name(aid)
        kind = ("cooldown" if aid in healing else "personal" if aid in personals
                else "trinket" if name in trinkets else None)  # potions: everyone's, below
        if not kind:
            continue
        tip = [f"{name} - {run.player_name(run.healer_id)} at {clock(t)}", where(t), hp_line(t)]
        if aid in cds.casts:
            prev = max((x for x in cds.casts[aid] if x < t), default=None)
            if prev is None:
                tip.append("First use - ready since the start.")
            else:
                back = cds.free_at(aid, prev)  # a second charge can be free right away
                tip.append(f"Came back at {clock(back)}; pressed {(t - back) / 1000:.0f}s later." if t > back + 1000
                           else "Pressed as soon as it came back.")
        markers.append({"t_s": rel(t), "kind": kind, "label": name, "tip": tip,
                        "icon": icons[0].get(aid) or icons[1].get(name)})
    # Each player's potions, healthstones and on-use trinkets, drawn on that player's own lane.
    uses = []
    for t, src, aid, _ in group_casts:
        name = run.ability_name(aid)
        kind = ("potion" if "Potion" in name else "healthstone" if "Healthstone" in name
                else "trinket" if name in all_trinkets.get(src, ()) else None)
        if kind:
            uses.append({"lane": src, "t_s": rel(t), "kind": kind, "name": name,
                         "icon": icons[0].get(aid) or icons[1].get(name),
                         "tip": [f"{name} - {pname(src)} at {clock(t)}", where(t), hp_line(t)]})

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
            if r.get("note"):
                tip.append(f"ℹ {r['note']}")
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
            names: dict[str, set] = {}
            for s in up:
                names.setdefault(s["name"], set()).add(s["target"])
            up_text = ", ".join(f"{n} ({'everyone' if len(ts) >= len(players) else ', '.join(sorted(pname(x) for x in ts))})"
                                for n, ts in sorted(names.items()))
            # Your healing cooldowns lead the judgement; other players' defensives are context only.
            mine = [(t, a) for a, ts in cds.casts.items() for t in ts if -OWN_CD_BEFORE_MS <= t - h.t <= OWN_CD_AFTER_MS]
            ready = [n for a, (n, _) in healing.items()
                     if (last := max((t for t in cds.casts.get(a, []) if t <= h.t), default=None)) is None
                     or h.t - last >= cds.cd_ms.get(a, 0)]
            if mine:
                own_text = "Your cooldowns: " + ", ".join(
                    f"{run.ability_name(a)} ({abs(t - h.t) / 1000:.0f}s {'before' if t <= h.t else 'after'})" for t, a in sorted(mine))
            else:
                own_text = ("Your cooldowns: none used - ready: " + ", ".join(ready)) if ready else \
                           "Your cooldowns: none used (all on cooldown)"
            top_pre = prof.hit_pre.get(h.ability) if prof else None
            tip = [f"{run.ability_name(h.ability)} at {clock(h.t)} - hit {h.targets} players for ~{h.avg_frac:.0%} "
                   f"of max HP each", where(h.t),
                   f"Group {pct(h.pre_ehp)} before → {pct(after)} after"
                   + (f"; back up in {h.recovery_s:.0f}s" if h.recovery_s else "; not back up within 30s"),
                   f"Peers held {pct(top_pre)} before this hit" if top_pre is not None else "",
                   own_text,
                   ("Also up (others): " + up_text) if up else "Others: no defensives up"]
            tag = avoid.tag_at(run, h.ability, h.t) if avoid else None
            if tag:
                tip.insert(1, f"⚠ {tag.label} - {tag.evidence}")
            if encounter and (note := encounter.note(h.ability, run.ability_name(h.ability))):
                tip.insert(1, f"ℹ {note}")
            # Highlight big hits that went through with none of YOUR cooldowns while one was ready - other players'
            # defensives don't clear it (they never weigh like a healer cooldown in heavy damage).
            bare = h.avg_frac >= UNCOVERED_MIN_HIT and not mine and bool(ready) and after < UNCOVERED_LOW_EHP
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
    for name, stored in (state.stored if state else {}).items():  # stored heals: when one was being kept
        spans_s = [[rel(a), rel(b)] for a, b in stored if b > a]
        ready_lanes.append({"name": spec.short_names.get(name, name), "ready": spans_s,
                            "ready_s": round(sum(b - a for a, b in spans_s))})
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
    # Short-cooldown, near-rotational defensives (Feint, Prismatic Barrier...) are "minor": hidden by default.
    defensives = load_defensives()
    minor = lambda n: defensives.get(n, {}).get("minor", defensives.get(n, {}).get("cd", 999) < MINOR_CD_S)
    defense = [{"lane": s["target"], "name": s["name"], "scope": s["scope"], "start_s": rel(s["start"]),
                "end_s": rel(s["end"]), "protective": s["protective"], "icon": icons[1].get(s["name"]),
                "minor": minor(s["name"]),
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
                      "top_curve": _top_curve(seg, tops),  # peers (comparable logs at your key level)
                      # top players (season study); approx = borrowed from the most similar studied pack
                      "study_curve": (study_curves[i][0] or {}).get("hp_curve") if study_curves else None,
                      "study_approx": bool(study_curves and study_curves[i][1]),
                      "phases": [{"name": ph.name, "start_s": rel(ph.start), "end_s": rel(ph.end)} for ph in phases.get(i, [])]})
    mana = [state.mana_at(tl.start + b * tl.bin_ms) for b in range(len(tl.group_ehp))] if state and state.mana else []
    # For "Analyze selection" on the page: damage taken, avoidable hits and your casts, per second.
    nbins = len(tl.group_ehp)
    casts_per_bin = [0] * nbins
    for t, _, _ in run.casts:
        casts_per_bin[tl.bin_of(t)] += 1
    avoid_hits: dict[tuple[int, str], float] = {}
    if avoid:
        for d in run.damage:
            tag = avoid.tag_at(run, d[2], d[0])
            if tag:
                k = (tl.bin_of(d[0]), tag.ability)
                avoid_hits[k] = avoid_hits.get(k, 0.0) + d[3] + d[4]
    return {
        "bin_s": tl.bin_ms / 1000,
        "group": [round(v, 3) for v in tl.group_ehp],
        "lowest": [round(v, 3) for v in tl.min_ehp],
        "mana": [None if m is None else round(m, 3) for m in mana],
        "pulls": pulls,
        "markers": sorted(markers, key=lambda m: m["t_s"]),
        "lanes": lanes,
        "defense": defense,
        "uses": uses,
        "intake": [round(v) for v in tl.intake],
        "intake_frac": [round(v, 4) for v in tl.intake_frac],
        "avoid_hits": [[b, n, round(a)] for (b, n), a in sorted(avoid_hits.items())],
        "casts": casts_per_bin,
        "uncovered_hits": uncovered,
        "lust": lust,
        "ready_lanes": ready_lanes,
    }
