from app.specs.base import Maintenance, Spec, SpellPair

# From 6 top +21-24 logs (Oct 2026): Stasis ~every 2 min, Rewind ~every 8 min, Obsidian Scales ~17 casts/run.
# Echo is what goes out before hits. Living Flame (filler) vs Emerald Blossom (group, lands ~2s after the cast).
SPEC = Spec(
    key="preservation",
    class_name="Evoker",
    spec_name="Preservation",
    label="Preservation Evoker",
    plural="Preservation Evokers",
    cooldowns={
        363534: ("Rewind", 240, "healing"),
        370537: ("Stasis", 90, "healing"),
        357170: ("Time Dilation", 60, "utility"),
        374227: ("Zephyr", 120, "utility"),
        370553: ("Tip the Scales", 120, "utility"),
        363916: ("Obsidian Scales", 90, "personal"),
    },
    maintenance=Maintenance(buff_id=364343, name="Echo", plural="Echoes", duration_s=15.0),
    spell_pair=SpellPair(single="Living Flame", group="Emerald Blossom",
                         land_ms={"Living Flame": 500, "Emerald Blossom": 2500}),
    tracked_buffs=("Essence Burst", "Temporal Compression", "Tip the Scales", "Lifespark"),
    mana_spells="Emerald Blossom and Dream Breath",
    personal_habit="press Obsidian Scales about every 90 seconds",
)
