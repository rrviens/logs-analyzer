"""Encounter notes (data/encounters.json): our own short note per important enemy ability, written from log
evidence. Notes are matched by spell id, then by name (one ability often
has several ids)."""
import json
from dataclasses import dataclass, field

from app.config import PROJECT_ROOT

PATH = PROJECT_ROOT / "data" / "encounters.json"


@dataclass
class Encounter:
    by_id: dict = field(default_factory=dict)
    by_name: dict = field(default_factory=dict)
    heal_checks: list = field(default_factory=list)  # bosses you heal (Avatar of Sethraliss), marked by hand

    @property
    def covered(self) -> bool:
        return bool(self.by_id)

    def entry(self, ability_id: int | None = None, name: str | None = None) -> dict | None:
        return self.by_id.get(str(ability_id)) if ability_id is not None and str(ability_id) in self.by_id \
            else self.by_name.get(name or "")

    def note(self, ability_id: int | None = None, name: str | None = None) -> str | None:
        e = self.entry(ability_id, name)
        return e["note"] if e else None

    def avoidable(self, ability_id: int | None, name: str) -> bool:
        """Marked as dodgeable in the notes (ground effects, things to walk out of)."""
        e = self.entry(ability_id, name)
        return bool(e) and e.get("avoidable") is True

    def no_kick(self, ability_id: int | None, name: str) -> bool:
        """Interruptible, but groups let it through on purpose (a tank reflects it) - never a missed kick."""
        e = self.entry(ability_id, name)
        return bool(e) and e.get("no_kick") is True

    def not_avoidable(self, ability_id: int, name: str) -> bool:
        e = self.entry(ability_id, name)
        return bool(e) and e.get("avoidable") is False


def load_encounter(dungeon: str) -> Encounter:
    if not PATH.exists():
        return Encounter()
    d = json.loads(PATH.read_text(encoding="utf-8")).get("dungeons", {}).get(dungeon)
    if not d:
        return Encounter()
    by_id = d.get("abilities", {})
    return Encounter(by_id, {e["name"]: e for e in by_id.values()}, d.get("heal_checks", []))
