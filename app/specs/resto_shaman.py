from app.specs.base import Maintenance, Spec, SpellPair

# Season 2 Midnight study (docs/context_model.md): top players press HTT every ~120s and SLT every
# ~180s; Ascendance and Mana Tide don't appear in top runs.
SPEC = Spec(
    key="resto_shaman",
    class_name="Shaman",
    spec_name="Restoration",
    label="Resto Shaman",
    plural="Resto Shamans",
    cooldowns={
        108280: ("Healing Tide Totem", 120, "healing"),
        98008: ("Spirit Link Totem", 180, "healing"),
        114052: ("Ascendance", 180, "healing"),
        79206: ("Spiritwalker's Grace", 120, "utility"),
        108271: ("Astral Shift", 90, "personal"),  # top players: ~13 casts/run, shortest gap 90s
    },
    maintenance=Maintenance(buff_id=61295, name="Riptide", plural="Riptides", duration_s=18.0),
    # Healing Wave hits a second target this patch; Chain Heal lands a second set of bounces ~0.7s later.
    spell_pair=SpellPair(single="Healing Wave", group="Chain Heal",
                         land_ms={"Healing Wave": 500, "Chain Heal": 1500},
                         empower_cast="Unleash Life", instant_cast="Nature's Swiftness"),
    # Found in Season 2 Midnight logs; High Tide no longer exists.
    tracked_buffs=("Tidal Waves", "Unleash Life", "Ascendance", "Coalescing Water", "Nature's Swiftness"),
    proc_buffs=("Nature's Guardian",),
    # Apex talent: Riptide procs (6%) and every Nature's Swiftness store a Stormstream Totem (buff 1267089, cast
    # 1267068). Its burst lands ~3-5% of a ~1M HP bar on 2 allies, plus the stream (measured in Season 2 logs).
    stored_cooldowns={1267089: ("Stormstream Totem", {"heal": 0.05, "hps": 0.006}, 10)},
    short_names={"Healing Tide Totem": "Healing Tide", "Spirit Link Totem": "Spirit Link", "Stormstream Totem": "Stormstream"},
    mana_spells="Chain Heal and Healing Rain",
    personal_habit="press Astral Shift ~13 times a run - often before targeted damage lands",
    dispels=("magic", "curse"),
    self_res=("Reincarnation",),
    dps_aoe="Chain Lightning",
    dps_st=("Lava Burst", "Lightning Bolt"),
)
