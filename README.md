# wow-log-analyzer

Context-aware M+ healer analysis on top of the Warcraft Logs API.

Supported specs: Restoration Shaman, Restoration Druid, Mistweaver Monk, Holy Priest, Preservation Evoker, Holy Paladin
and Discipline Priest (beta, see Status). Each is a module in `app/specs/`, built from what top players of that spec actually cast in
current-patch logs. Spec rules that need a top-player study (planned cooldown timings, cooldown rate) work once
`python study.py --spec <key>` has been run for that spec; Resto Shaman's study is included.

## Setup

```
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Put your WCL API client in `.env` (see `.env.example`).

## Use

Double-click **`start.bat`**. It sets up the venv on first run, starts the app and opens
http://127.0.0.1:8000. Set your character once (top right); your recent keys are listed
(duplicates from other uploaders and short bricked keys hidden). Click a key to analyze it, or paste any log URL.
Results are saved, so reopening a key is instant; "Re-analyze" forces a fresh run.

A fresh analysis costs ~60–200 WCL API points (1000/hour), depending on how many top logs are already cached.

Command line, if you prefer:

```
.\.venv\Scripts\python cli.py analyze "<report url>" [--player NAME] [--top 8] [--all] [--fresh]
```

## Hosting it for others (sign in with Warcraft Logs)

In hosted mode (`AUTH_MODE=wcl`) every visitor signs in with their own Warcraft Logs account (OAuth with PKCE).
Their analyses run on their own token and API limit, their private logs are cached under their own account only,
and settings and jobs are per visitor. Ranked top logs are public and fetched once into a shared cache.
The top-player study is shared too: the site owner runs `python study.py --spec <key>` and deploys the result.

**Recommended host: [Render](https://render.com)** - deploys from GitHub with the included `render.yaml`
(Docker, health check on `/healthz`, a 1 GB persistent disk at `/data`). The Starter plan plus the disk is roughly
$7-8/month; the free tier has no persistent disk, so the cache and sign-ins would be lost on every restart.

1. In your Warcraft Logs API client (https://www.warcraftlogs.com/api/clients) add the redirect URL
   `https://<your-service>.onrender.com/callback` (keep `http://localhost:8000/callback` for local testing).
2. On Render: New > Blueprint > pick this repository. Fill in the secret values when asked:
   `WCL_CLIENT_ID`, `WCL_CLIENT_SECRET`, `REDIRECT_URI` (the URL from step 1). They live only in Render's settings.
3. Deploy. The site shows "Sign in with Warcraft Logs"; after signing in, set your character as usual.

Run it as one instance (jobs are in memory, the cache is SQLite). Any Docker host works the same way: build the
`Dockerfile`, mount a persistent volume at `/data`, set the three environment variables, serve over https.

To try hosted mode locally: set `AUTH_MODE=wcl` and `SECURE_COOKIES=0` in `.env` and start the app as usual.

## On the page

- **Timeline** per pull: group HP, lowest player, top groups' typical HP, boss phases, lanes for healing
  cooldowns / personals / trinkets & potions / big group hits / deaths / findings, and your mana. Hover for the
  moment; "Show on timeline" on a finding jumps there; clicking a finding dot jumps to the finding.
- **Boss phases** are learned from one-off boss casts that start a new part of the fight in most runs
  (e.g. Lightwarden Ruia's Shapeshift: Bear / Haranir). Findings carry the phase, and "Group lower than top
  groups in P3" compares each phase against the same phase in the top logs.
- **Buffs & mana**: Tidal Waves, Unleash Life, Ascendance, Coalescing Water and Nature's Swiftness are tracked
  at each heal (`tracked_buffs` in the spec module, e.g. `app/specs/resto_shaman.py`), plus your mana. Low mana during danger is flagged.
- **What to look at per pull**: picking a pull (dropdown or a click on the overview) shows only the findings from
  that pull; "Show full dungeon" brings back the whole list. Time links zoom without filtering.
- **Top-player study** button (header): shows the study's age; click to refresh it with 10 runs per dungeon.
  It pauses for the hourly API reset when the budget gets low (keeps 150 points free for your own keys).

## Death recaps

Every death (up to 10 per run) gets a card: what was ready from the **group**, the **player who died** and **you
(healer)**, each ready defensive with an estimate - "Likely lived ~X%", "Too close to call" or "Would still die" -
and the last 10 seconds of hits and heals with HP after each. The estimate replays those seconds from the real HP,
adding only what the defensive would have changed (damage reduction, extra max HP, shield, heal), pressed just in
time; magic-only / physical-only defensives only count against that school. Defensive values and cooldowns are in
`data/defensives.json` - approximate, edit them for the current patch. Abilities never pressed in the run are listed
separately ("may not be talented").

Death findings name the real cause by damage share over the last 10s (constant pulses are background), and when a
debuff was behind it: how long it sat, who else it hit and how they cleared it, where your dispels went, which of
the victim's tools clear it (learned from your run and the top logs - escapes, immunities, dispels only), and how
top healers of your spec handle the same debuff. "Nothing was ready" always says why (when each cooldown was used and on
what, procs already spent).

## Avoidable damage

All damage is unavoidable unless tagged (`app/avoidable.py`), learned from the same-level top logs per matched pull:

- **Avoidable**: the enemy ability exists in the top logs, but top groups were hit by it in at most a quarter of their
  runs (≤0.3 hits/player) - while your group was hit. Evidence: "top groups: hit in 0 of 8 runs · your group: 0.8 hits/player".
- **Avoidable (extra ticks)**: top groups take it too, but your group took 3x+ their ticks for a pull that long.
- **Went through**: an enemy cast top groups stop 70%+ of the time completed. Evidence: stop rate, which interrupts
  (Wind Shear, Pummel, Kick... - `INTERRUPTS`) were off cooldown, and CC top groups also use on it.

Abilities that only ever hit one player on several pulls are skipped (a player's own effect, e.g. Refraction).
Tags show on timeline hit markers (orange dot; ◆ for a cast that went through), overview pull hover, death recaps,
and the vs-top-player comparison. Healing findings (not topped, slow recovery, phase gaps) where 20%+ of the damage
around them was avoidable say "not a healing problem" and score lower.

## Chain pulls

A pull counts as chained when it starts ≤5s after the last one ended and the group was still taking damage (or the old
pack's abilities keep hitting after the new pull starts) - `app/chains.py`. The timeline marks it ⛓ (orange when it
went in risky: damage-over-time debuffs from the old pack still ticking, group 10%+ under top groups at that pull's
start, or your mana under 35%; cooldowns on cooldown are listed as context). The hover says whether top groups chain
at the same spot and what happened in the next 20s. A finding only fires when a risky chain then hurt (a death,
someone under 25% or the group under 50%).

## When Warcraft Logs is slow or rate limiting

- Finished analyses are stored (SQLite on the data disk). Opening an analyzed run makes no Warcraft Logs calls. When
  the rules change, the stored version shows at once ("Updating to the latest rules...") while a new one is made.
- New analyses are queued jobs in the database (`app/jobs.py`): one worker, survives restarts. A rate limit puts the
  job in "waiting" with a retry time (WCL's Retry-After, else 1, 2, 5, 10, 15, 30 min) instead of failing.
- Signing in and page loads need no Warcraft Logs calls; a session lasts as long as the WCL token (logged at sign-in).
- Signed out, a link to a public run opens its saved analysis; analyzing new runs or private logs needs a sign-in.
- The recent-keys list falls back to the last saved list with a quiet note, and refreshes itself.
- Every failure is logged with context (route or job, report/fight, reason, a short ref) - never tokens or codes.

## Healing overlaps

Your own healing cooldowns are the critique point; other players' defensives are shown as context and never weigh
the same. An overlap is two or more of your cooldowns within 5s of one big hit while the group stayed healthy (above
60%, lowest player above 40%, nobody died) - one would likely have done. When the next big hit lands while one is
still down and someone drops below the lethal line or dies, that is its own finding ("Healing overlap left the next
spike uncovered"). Your cooldown on top of another player's group defensive only counts, at lower weight, when it
cost you the next spike. Harmless overlaps are summed up in one low-priority line (from 3); ones top players also do
there (planned timings) are skipped unless they hurt. Thresholds: OVERLAP_* in `app/findings.py`.

On the timeline, a big hit is red when one of your cooldowns was ready, none was used, and the group dropped below
60%; its tooltip leads with your cooldowns and lists other players' defensives as "Also up".

## DPS windows

Calm stretches (3s+) with almost no damage coming in, nobody low, no big group hit in the next 3s, and either the group
healthy or healing already running (totems, HoTs - HP will climb on its own) - time a healer can spend on damage.
Missed damage = what you could have cast there minus what you dealt: your own average Chain Lightning hit × enemies
alive (up to 3, 1.7s cast) on 2+ enemies, your own Lava Burst / Lightning Bolt on one. Enemies alive = distinct
enemies casting around the window (a floor - pure melee mobs don't show). Specs without damage spells set fall back to
top healers' damage per second in their own calm moments, which is also shown alongside. The "DPS windows" section
has its own whole-run strip that shades each one (click a window to open it on the timeline): green = used, amber =
partly, red = mostly missed; the biggest missed windows are listed with clickable times. Thresholds are at the top of
`app/dps_windows.py`, spell fields in the spec (`dps_aoe`, `dps_st`).

## Encounter notes

`data/encounters.json` holds our own one-line note per important enemy ability (type - tank damage, unavoidable
group damage, targeted, dispellable, interruptible, ticking - and what a healer should do), written from log evidence
across cached top runs, plus a Wowhead guide link per dungeon. Notes show in timeline hovers (ℹ) and death recaps;
`"avoidable": false` overrides a wrong learned "avoidable" tag. Currently covered: The Blinding Vale, Den of Nalorakk,
Ruby Life Pools (the dungeons with enough cached logs); the others get notes as logs come in. Edit freely.

## Data quality

Top logs are checked before they become a baseline (`app/quality.py`): HP data on most hits, real (changing) HP for
all 5 players, a group HP line that moves, and not a partial upload. Failing logs are skipped and replaced from
further down the rankings; the page says how many were skipped. The study applies the same gate. The timeline's
top-group line is a curve - what top groups' HP did through the same pull (median, middle half shaded).

## Reading the page

- Every finding, death recap and section verdict is one line with a "→ fix" chip; click to expand the full analysis.
  "Expand all / Collapse all" per section; a sticky section nav jumps between categories.
- Every time (13:44, 13:42-13:52) is a link: it zooms the timeline to that moment (or range) and marks it.
- 📌 Pin keeps a compact timeline on screen while you read (remembered per browser); "Hide chart" folds it.
- Whole-run overview strip above the timeline: every pull side by side (bosses tinted, gaps between pulls squeezed),
  group HP, deaths, your cooldowns, trinkets/potions, ⚡ Bloodlust, and green lanes for when Healing Tide / Spirit Link
  / Astral Shift sat ready in combat. Hover a pull for its one-line verdict; click it or drag a range to zoom the detail.
- "Whole run" (first entry in the pull selector, the "Show whole run" button, or double-click the overview) shows the
  entire key in the detail timeline with every lane, gaps squeezed like the overview. Markers closer than a few pixels
  merge into one (drawn as the most important, with a count and a tooltip listing them); click a pull to open it.
  The badge next to "Timeline" always says what is shown.
- Timeline: hover any marker or bar for details (who, when, numbers). One lane per party member shows active
  personals / externals / group defensives from real buff uptimes; big hits with nothing up are shaded red.
  Each player's lane also shows their potions, healthstones and on-use trinkets (outlined icons).
- Timeline zoom and selection: Ctrl+wheel (or trackpad pinch) zooms around the cursor, drag pans when zoomed,
  double-click resets. Drag (Shift+drag when zoomed) selects a range: in a pull view it zooms to exactly it; in the whole run it stays shaded (it may span pulls - gaps between pulls are left out of the numbers; "Zoom to it" when it's inside one pull): "Selection" sums up group HP vs top groups,
  damage taken and its avoidable share, your casts and cooldowns, defensives, potions, deaths and findings in it.

## Group composition

The page shows the group with each spec and the group / external cooldowns each player brings (from
`data/defensives.json`): how often pressed and how many big group hits one was up for; "never pressed" and "never on
a big hit" are flagged. Area effects: Anti-Magic Zone counts only players who got its buff (stood in it); Darkness and
Aura Mastery leave no buff on the party, so the whole party counts for their duration after the cast (`from_cast`).
Checked against 51 logs; Power Word: Barrier was never seen (kept), Mass Barrier, Ancestral Protection Totem,
Roar of Sacrifice and Fortitude of the Bear were never seen (not added).

What a spec brings to damage taken lives in `data/comp_effects.json`
(edit freely). Entries with `"reduces_intake": true` (e.g. Augmentation Evoker) make damage-taken
comparisons use only top groups with the same setup when there are at least 2, and say so when none had it.

## Healing Wave vs Chain Heal

Each Healing Wave / Chain Heal is tagged with the situation just before it landed (0, 1, 2 or 3+ players
under 80%) and what it actually did (every target hit - Healing Wave hits two this patch - effective vs overheal,
players only). The page compares your Chain Heal share and overheal per situation with the comparable top logs.
Findings only fire for stretches that clearly mattered (3+ Chain Heals while one player was dying, or 3+ Healing
Waves through sustained group damage) and only when the top players' pattern supports it. Positions and mana are
not modeled. This pattern comes from the same-level top logs of each analysis, not from `study.py` (which skips
healing events to stay inside the API budget).

## Context model (what top players do)

```
.\.venv\Scripts\python study.py --spec resto_shaman --runs 5
```

Studies the top-ranked runs of one healer spec in every dungeon of the current season and writes
`data/context_models/<spec>.json` (used by the analyzer) and a readable `docs/context_models/<spec>.md`:
cooldowns per pull and their planned boss timings, recurring group hits, Riptides out before them,
what top players answer them with, and how many deaths even top groups take per pull.
Re-run after a patch. ~13 API points per studied run.

## How it judges a run

1. **HP timeline** - per-second group and per-player HP (incl. absorbs), rebuilt from damage snapshots and healing.
2. **Profiles** - each pull is matched to the same pull in top logs of your spec at the same key level
   (boss by encounter, trash by enemy set). "Topped" is what those groups actually hold there - not a fixed number.
3. **Context** - every second is labelled calm / pressure / spike / lethal against that profile. Team defensives
   are detected from a jump in damage mitigation.
4. **Findings** - only fire when the moment mattered:
   - deaths: one-shot vs. healable, cooldowns ready vs. pressed too late; a wipe is reported once, from its first death,
     with what each group cooldown was doing during it
   - group cooldowns held only through *sustained* group danger (2+ players under 50% for 3s+, and someone under 30%
     or 5s+) - a dip healed back within a couple of GCDs is not flagged
   - missed planned boss cooldowns (from the context model), only when damage followed
   - you low with Astral Shift ready (skipped right after Reincarnation)
   - not topped / Riptide prep before recurring group hits, slow recovery under normal intake, extra avoidable damage

Thresholds live at the top of `app/context.py` and `app/findings.py`. Spell IDs and spec-specific mechanics live in the spec modules under `app/specs/`.

## Adding a healer spec

A spec is one `Spec` (`app/specs/base.py`): cooldowns (healing / utility / personal), the maintenance HoT
(Riptide for Resto Shaman), the single-target vs group heal pair, tracked buffs, procs and wording. Add a module with a
`SPEC` and list it in `app/specs/__init__.py`. The spec is detected from the log (the analyzed player's class and spec).

## Known limits

- Cooldowns never cast in a run are treated as not talented.
- Boss phases are not split yet; recurring group hits stand in for them.
- WCL API budget is 1000 points/hour; a run costs ~23 points and is cached in `cache.sqlite3`.

## Status (parked work)

- **Discipline Priest** (`app/specs/discipline.py`): written from 6 top logs - Atonement (buff 194384, verified) as the
  "out before big hits" mechanic, Evangelism and Ultimate Penitence as ramp cooldowns, no single-vs-group heal pair.
  The full trial analysis on a top Disc log has not run yet (parked to save API budget), so treat its findings as beta.
- **Top-player studies** for Resto Druid, Mistweaver, Holy Priest, Preservation, Holy Paladin and Discipline are not run
  yet (~520 API points per spec at 5 runs per dungeon): `python study.py --spec <key> --runs 5`. Until then those specs
  have no planned-cooldown-timing or cooldown-rate findings.
