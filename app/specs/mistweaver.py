from app.specs.base import Maintenance, Spec

# From 6 top +21-24 logs (Oct 2026): most healing is passive (Ancient Teachings, Way of the Crane) and
# nobody casts Vivify, so there is no single-vs-group pair to judge. Chi-Ji ~every 90s, Revival ~every 6 min.
SPEC = Spec(
    key="mistweaver",
    class_name="Monk",
    spec_name="Mistweaver",
    label="Mistweaver",
    plural="Mistweavers",
    cooldowns={
        115310: ("Revival", 180, "healing"),
        325197: ("Invoke Chi-Ji, the Red Crane", 60, "healing"),
        443028: ("Celestial Conduit", 90, "healing"),
        116849: ("Life Cocoon", 120, "utility"),
        116680: ("Thunder Focus Tea", 30, "utility"),
        115203: ("Fortifying Brew", 90, "personal"),
    },
    maintenance=Maintenance(buff_id=119611, name="Renewing Mist", plural="Renewing Mists", duration_s=20.0),
    tracked_buffs=("Thunder Focus Tea", "Teachings of the Monastery", "Chi-Ji's Swiftness", "Mana Tea"),
    short_names={"Invoke Chi-Ji, the Red Crane": "Chi-Ji"},
    mana_spells="Enveloping Mist and Sheilun's Gift",
    personal_habit="press Fortifying Brew about every 3 minutes",
    dispels=("magic", "poison", "disease"),
    dps_aoe="Spinning Crane Kick",
    dps_aoe_targets=5,
    dps_aoe_cast_s=1.5,
    dps_aoe_ticks=4,  # each enemy is hit ~4 times per channel
    dps_st=("Rising Sun Kick", "Blackout Kick", "Tiger Palm"),
    dps_st_cast_s=1.2,
)
