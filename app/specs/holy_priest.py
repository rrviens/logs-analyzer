from app.specs.base import Maintenance, Spec

# From 6 top +21-24 logs (Oct 2026): Flash Heal is the only filler (no Heal / Prayer of Healing casts), so
# there is no single-vs-group pair to judge. Prayer of Mending (~240 casts/run) is what goes out before hits.
SPEC = Spec(
    key="holy_priest",
    class_name="Priest",
    spec_name="Holy",
    label="Holy Priest",
    plural="Holy Priests",
    cooldowns={
        200183: ("Apotheosis", 120, "healing"),
        64843: ("Divine Hymn", 120, "healing"),
        47788: ("Guardian Spirit", 180, "utility"),
        32375: ("Mass Dispel", 120, "utility"),
        19236: ("Desperate Prayer", 90, "personal"),
    },
    maintenance=Maintenance(buff_id=41635, name="Prayer of Mending", plural="Prayers of Mending", duration_s=30.0),
    tracked_buffs=("Surge of Light", "Benediction", "Apotheosis"),
    mana_spells="Flash Heal and Prayer of Mending",
    personal_habit="press Desperate Prayer about every 3 minutes",
    dispels=("magic", "disease"),
    dps_aoe="Holy Nova",
    dps_aoe_targets=5,
    dps_aoe_cast_s=1.5,
    dps_st=("Holy Fire", "Smite"),
)
