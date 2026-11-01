"""What the shared analysis needs to know about one healer spec. Everything else (timeline, context,
deaths, defensives, avoidable damage, comparisons) is spec-independent.

Spell names and IDs come from current-patch logs, not memory - verify them when a patch lands.
Effective cooldowns are also learned from each log (shortest gap between casts)."""
from dataclasses import dataclass, field

# On-use racials, matched by cast name (stable across expansions). Shared by every spec.
RACIALS = {
    "Berserking", "Blood Fury", "Arcane Torrent", "Fireblood", "Ancestral Call", "Gift of the Naaru",
    "Stoneform", "Will to Survive", "Every Man for Himself", "Shadowmeld", "War Stomp", "Quaking Palm",
    "Bag of Tricks", "Rocket Barrage", "Light's Judgment", "Arcane Pulse", "Haymaker", "Bull Rush",
    "Hyper Organic Light Originator", "Regeneratin'", "Azerite Surge", "Darkflight", "Spatial Rift",
}


@dataclass(frozen=True)
class Maintenance:
    """The HoT a spec keeps rolling and prepares big hits with (Riptide for Resto Shaman)."""
    buff_id: int
    name: str
    plural: str
    duration_s: float
    pandemic: float = 0.3  # share of the duration that carries over on refresh


@dataclass(frozen=True)
class SpellPair:
    """A single-target heal vs a group heal, judged by how many players were injured at the cast."""
    single: str
    group: str
    land_ms: dict  # spell name -> how long after the cast its heals can still land (bounces, cleave)
    empower_cast: str = ""  # a cast that empowers the next heals (Unleash Life)
    empower_window_ms: int = 10_000
    instant_cast: str = ""  # a cast that makes the next heal instant (Nature's Swiftness) - excused from timing rules


@dataclass(frozen=True)
class Spec:
    key: str  # file-safe id, e.g. "resto_shaman"
    class_name: str  # as Warcraft Logs names it in rankings ("Shaman", "DeathKnight")
    spec_name: str  # as Warcraft Logs names it ("Restoration")
    label: str  # short, for text: "Resto Shaman"
    plural: str  # "Resto Shamans" - "Top Resto Shamans press it..."
    cooldowns: dict  # ability id -> (name, base cooldown s, kind: healing / utility / personal)
    maintenance: Maintenance | None = None
    spell_pair: SpellPair | None = None
    tracked_buffs: tuple = ()  # healer buffs that change how heals behave
    proc_buffs: tuple = ()  # passive cheat-death / emergency procs (logged as buff or heal)
    short_names: dict = field(default_factory=dict)  # long cooldown name -> short, for one-line findings
    mana_spells: str = ""  # "Chain Heal and Healing Rain" - what low mana limits
    personal_habit: str = ""  # how top players use the personal, e.g. "press Astral Shift ~13 times a run"
    self_res: tuple = ()  # abilities that bring the healer back (low HP right after is not incoming damage)
    ramp_cooldowns: tuple = ()  # planned ramps (Evangelism): judged by top players' timings, not "held through danger"
    dps_aoe: str = ""  # damage spell for 2+ enemies in calm windows, e.g. "Chain Lightning"
    dps_aoe_targets: int = 3  # enemies it hits
    dps_aoe_cast_s: float = 1.7
    dps_aoe_ticks: int = 1  # hits on each enemy per cast (channels like Spinning Crane Kick tick several times)
    dps_st: tuple = ()  # single-target damage spells, e.g. ("Lava Burst", "Lightning Bolt")
    dps_st_cast_s: float = 1.5  # average time per single-target cast

    @property
    def healing_cooldowns(self) -> dict:
        return {aid: (n, cd) for aid, (n, cd, kind) in self.cooldowns.items() if kind == "healing"}

    @property
    def personal_cooldowns(self) -> dict:
        return {aid: (n, cd) for aid, (n, cd, kind) in self.cooldowns.items() if kind == "personal"}

    @property
    def major_pair_text(self) -> str:
        """'Healing Tide or Spirit Link' - the two biggest healing cooldowns, short names."""
        names = [self.short_names.get(n, n) for n, _ in list(self.healing_cooldowns.values())[:2]]
        return " or ".join(names)
