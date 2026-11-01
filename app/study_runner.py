"""Runs the top-player study for one healer spec for the current season and writes its context model.

Budget-aware: before each run it checks the WCL hourly budget and, when it gets low,
waits for the reset instead of failing - so a 10-runs-per-dungeon study can run for
hours in the background. Fetched runs are cached, so a stopped study resumes cheaply.
"""
import json
import time
from datetime import datetime, timezone
from typing import Callable

from app.context_model import doc_path, model_path
from app.specs.base import Spec
from app.reports import get_fights, get_players
from app.study import TRASH_MATCH_MIN, build_model, cluster_segments, discover_cooldowns, fetch_light, hp_curve, top_runs
from app.wcl import WCLClient

ZONES_QUERY = "{ worldData { zones { id name encounters { id name } } } }"
RATE_QUERY = "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
BUDGET_RESERVE = 150  # keep this many points free for analyzing your own keys
DEFAULT_RUNS = 10


def run_study(client: WCLClient, spec: Spec, runs: int = DEFAULT_RUNS, dungeon: str | None = None,
              progress: Callable[[str], None] = print) -> dict:
    zones = [z for z in client.query(ZONES_QUERY, ttl=86400)["worldData"]["zones"] if "Mythic+" in z["name"]]
    zone = max(zones, key=lambda z: z["id"])
    dungeons = [e for e in zone["encounters"] if not dungeon or e["name"] == dungeon]

    def wait_for_budget():
        r = client.query(RATE_QUERY, ttl=0)["rateLimitData"]
        if r["limitPerHour"] - r["pointsSpentThisHour"] >= BUDGET_RESERVE:
            return
        wait = r["pointsResetIn"] + 5
        progress(f"API budget low ({r['pointsSpentThisHour']:.0f}/{r['limitPerHour']}) - pausing "
                 f"{wait // 60} min until the hourly reset.")
        time.sleep(wait)

    path, doc = model_path(spec), doc_path(spec)
    model = json.loads(path.read_text()) if path.exists() and dungeon else {"dungeons": {}}
    model["zone"] = zone["name"]
    all_runs, skipped = {}, {}
    for n, d in enumerate(dungeons, 1):
        progress(f"Studying {d['name']} ({n} of {len(dungeons)})...")
        skipped[d["name"]] = []
        all_runs[d["name"]] = top_runs(client, d["id"], runs, spec, before_fetch=wait_for_budget,
                                       skipped=skipped[d["name"]])

    cooldowns = discover_cooldowns([r for rs_ in all_runs.values() for r in rs_])
    model["cooldowns"] = {str(k): v for k, v in cooldowns.items()}
    for d in dungeons:
        model["dungeons"][d["name"]] = {"encounter_id": d["id"], **build_model(all_runs[d["name"]], cooldowns),
                                        "skipped": skipped[d["name"]]}
    model["generated"] = datetime.now(timezone.utc).isoformat()
    model["runs_per_dungeon"] = runs

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=1))
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(render_doc(model, spec), encoding="utf-8")
    progress(f"Done: {sum(len(v) for v in all_runs.values())} top runs studied, "
             f"{sum(len(v) for v in skipped.values())} skipped for incomplete data.")
    return model


def add_curves(client: WCLClient, spec: Spec, progress: Callable[[str], None] = print) -> int:
    """Top-player HP bands for an existing study, rebuilt from its cached runs (no new API calls when cached).
    Returns how many segments got a band."""
    path = model_path(spec)
    if not path.exists():
        return 0
    model, done = json.loads(path.read_text()), 0
    for name, d in model.get("dungeons", {}).items():
        runs = []
        for r in d.get("runs", []):
            try:
                fight = next(f for f in get_fights(client, r["code"]) if f.id == r["fight"])
                me = next(p for p in get_players(client, fight) if p.name == r["player"])
            except StopIteration:
                continue
            runs.append(fetch_light(client, fight, me.id, r["player"], spec))
        clusters = cluster_segments(runs)
        for seg in d.get("segments", []):
            def same(c):
                if seg["encounter_id"]:
                    return c["encounter_id"] == seg["encounter_id"]
                a, b = set(c["npc_ids"]), set(seg["npc_ids"])
                return not c["encounter_id"] and a and b and len(a & b) / len(a | b) >= TRASH_MATCH_MIN
            c = next((c for c in clusters if same(c)), None)
            seg["hp_curve"] = hp_curve(runs, c["members"]) if c else None
            done += seg["hp_curve"] is not None
        progress(f"{spec.label} {name}: HP bands for {sum(1 for s in d.get('segments', []) if s.get('hp_curve'))} pulls")
    path.write_text(json.dumps(model, indent=1))
    return done


def study_info(spec: Spec) -> dict:
    path = model_path(spec)
    if not path.exists():
        return {"exists": False, "spec": spec.label}
    m = json.loads(path.read_text())
    return {"exists": True, "spec": spec.label, "zone": m.get("zone"), "generated": m.get("generated"),
            "runs_per_dungeon": m.get("runs_per_dungeon"),
            "runs": {name: len(d["runs"]) for name, d in m.get("dungeons", {}).items()}}


def render_doc(model: dict, spec: Spec) -> str:
    hot = spec.maintenance.plural if spec.maintenance else "HoTs"
    lines = [f"# Context model - {spec.label} - {model['zone']}", "",
             f"Generated {model.get('generated', '?')[:10]} from top-ranked {spec.label} runs "
             f"(up to {model.get('runs_per_dungeon', '?')} per dungeon). Regenerate after patches.", "",
             "## Cooldowns top players treat as cooldowns", ""]
    for c in sorted(model["cooldowns"].values(), key=lambda c: c["name"]):
        lines.append(f"- **{c['name']}**: used in {c['runs_used']} runs, ~{c['casts_per_run']:g} casts/run, "
                     f"shortest gap {c['observed_cd_s']}s")
    for name, d in model["dungeons"].items():
        keys = ", ".join(f"{r['player']} +{r['key']}" for r in d["runs"])
        lines += ["", f"## {name}", "", f"Runs: {keys}", ""]
        for s in d["segments"]:
            kind = "boss" if s["encounter_id"] else "trash"
            lines.append(f"### {s['name']} ({kind}, ~{s['median_duration_s']}s, "
                         f"{s['deaths_per_run']:g} deaths/run in top runs)")
            for cd, u in sorted(s["cooldowns"].items(), key=lambda x: -x[1]["share_of_runs"]):
                planned = ", ".join(f"~{p['at_s']}s ({p['share']:.0%})" for p in u.get("planned", []))
                lines.append(f"- {cd}: {u['share_of_runs']:.0%} of runs, at {u['times_s']}s into the pull, "
                             f"{u['proactive_share']:.0%} pressed before the damage"
                             + (f"; planned timing {planned}" if planned else ""))
            for h in s["group_hits"][:5]:
                ans = ", ".join(f"{k} {v:.0%}" for k, v in h["answered_by"].items()) or "nothing"
                prep = ", ".join(f"{p['name']} (x{p['lift']})" for p in h["prep_casts"]) or "-"
                lines.append(f"- Hit **{h['name']}** ~{h['avg_hit_pct']}% HP x{h['per_run']}/run: group "
                             f"{h['pre_hp_median']:.0%} before (p25 {h['pre_hp_p25']:.0%}), "
                             f"{h['riptides_median']:g} {hot} out; answered by {ans}; prep casts: {prep}")
            lines.append("")
    return "\n".join(lines)
