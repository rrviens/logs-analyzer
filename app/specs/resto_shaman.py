# Spell IDs and base cooldowns. Verify against the current patch - talents and
# expansion changes can alter IDs, durations and cooldowns. Effective cooldowns
# are also learned from the log (shortest gap between casts).
CLASS_NAME = "Shaman"
SPEC_NAME = "Restoration"

RIPTIDE_ID = 61295
RIPTIDE_DURATION_S = 18.0
PANDEMIC_FRACTION = 0.3

# ability_id -> (name, base cooldown in seconds, kind)
# kind "healing" cooldowns are judged by the greed / justified / low-value rules.
# Season 2 Midnight study (docs/context_model.md): top players press HTT every ~120s
# and SLT every ~180s; Ascendance and Mana Tide don't appear in top runs.
COOLDOWNS = {
    108280: ("Healing Tide Totem", 120, "healing"),
    98008: ("Spirit Link Totem", 180, "healing"),
    114052: ("Ascendance", 180, "healing"),
    79206: ("Spiritwalker's Grace", 120, "utility"),
    108271: ("Astral Shift", 90, "personal"),  # top players: ~13 casts/run, shortest gap 90s
}

# On-use racials, matched by cast name (stable across expansions).
RACIALS = {
    "Berserking", "Blood Fury", "Arcane Torrent", "Fireblood", "Ancestral Call", "Gift of the Naaru",
    "Stoneform", "Will to Survive", "Every Man for Himself", "Shadowmeld", "War Stomp", "Quaking Palm",
    "Bag of Tricks", "Rocket Barrage", "Light's Judgment", "Arcane Pulse", "Haymaker", "Bull Rush",
    "Hyper Organic Light Originator", "Regeneratin'", "Azerite Surge", "Darkflight", "Spatial Rift",
}

HEALING_COOLDOWNS = {aid: (name, cd) for aid, (name, cd, kind) in COOLDOWNS.items() if kind == "healing"}
PERSONAL_COOLDOWNS = {aid: (name, cd) for aid, (name, cd, kind) in COOLDOWNS.items() if kind == "personal"}
