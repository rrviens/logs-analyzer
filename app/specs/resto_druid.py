from app.specs.base import Maintenance, Spec, SpellPair

# From 6 top +21-24 logs (Oct 2026): Convoke ~every 80s, Tranquility ~every 5 min, Barkskin ~every 95s.
# Rejuvenation is what top druids blanket before big hits (~9 applications/min, 21s with talents).
SPEC = Spec(
    key="resto_druid",
    class_name="Druid",
    spec_name="Restoration",
    label="Resto Druid",
    plural="Resto Druids",
    cooldowns={
        740: ("Tranquility", 180, "healing"),
        391528: ("Convoke the Spirits", 60, "healing"),
        102342: ("Ironbark", 90, "utility"),
        132158: ("Nature's Swiftness", 60, "utility"),
        22812: ("Barkskin", 60, "personal"),
    },
    maintenance=Maintenance(buff_id=774, name="Rejuvenation", plural="Rejuvenations", duration_s=21.0),
    # Wild Growth heals over ~7s; count its ticks with the cast. Regrowth: the direct heal.
    spell_pair=SpellPair(single="Regrowth", group="Wild Growth", land_ms={"Regrowth": 500, "Wild Growth": 7000},
                         instant_cast="Nature's Swiftness"),
    tracked_buffs=("Soul of the Forest", "Clearcasting", "Nature's Swiftness"),
    short_names={"Convoke the Spirits": "Convoke"},
    mana_spells="Wild Growth and Regrowth",
    personal_habit="press Barkskin about every 95 seconds",
    dispels=("magic", "curse", "poison"),
    dps_aoe="Starfire",
    dps_aoe_targets=3,
    dps_aoe_cast_s=2.25,
    dps_st=("Wrath", "Starsurge"),
)
