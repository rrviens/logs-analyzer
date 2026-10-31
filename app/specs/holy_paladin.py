from app.specs.base import Spec

# From 6 top +21-24 logs (Oct 2026): Light of Dawn is barely cast (~1/run), so there is no single-vs-group
# pair, and no HoT top paladins blanket before hits - those rules are skipped. Avenging Wrath ~every 2 min.
SPEC = Spec(
    key="holy_paladin",
    class_name="Paladin",
    spec_name="Holy",
    label="Holy Paladin",
    plural="Holy Paladins",
    cooldowns={
        31884: ("Avenging Wrath", 120, "healing"),
        31821: ("Aura Mastery", 180, "healing"),
        6940: ("Blessing of Sacrifice", 120, "utility"),
        633: ("Lay on Hands", 420, "utility"),
        1022: ("Blessing of Protection", 300, "utility"),
        498: ("Divine Protection", 60, "personal"),
        642: ("Divine Shield", 300, "personal"),
    },
    tracked_buffs=("Infusion of Light", "Divine Purpose", "Avenging Wrath"),
    mana_spells="Flash of Light and Holy Light",
    personal_habit="press Divine Protection about every 80 seconds",
)
