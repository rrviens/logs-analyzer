"""Group composition and what each spec brings (data/comp_effects.json, user-editable)."""
import json

from app.config import PROJECT_ROOT
from app.reports import Player

EFFECTS_PATH = PROJECT_ROOT / "data" / "comp_effects.json"


def load_effects() -> dict[str, dict]:
    if not EFFECTS_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(EFFECTS_PATH.read_text()).items() if not k.startswith("_")}


def effect_for(player: Player, effects: dict[str, dict]) -> dict | None:
    """Spec entry wins over class entry ('Augmentation Evoker' before 'Evoker')."""
    return effects.get(player.label) or effects.get(player.class_name)


def intake_modifiers(players: list[Player], effects: dict[str, dict]) -> set[str]:
    """Effect keys in this group that change how much damage it takes."""
    out = set()
    for p in players:
        for key in (p.label, p.class_name):
            if effects.get(key, {}).get("reduces_intake"):
                out.add(key)
    return out


def describe(players: list[Player], effects: dict[str, dict]) -> list[dict]:
    order = {"tank": 0, "healer": 1, "dps": 2}
    return [
        {"name": p.name, "class": p.class_name, "spec": p.spec, "role": p.role,
         "note": (effect_for(p, effects) or {}).get("note")}
        for p in sorted(players, key=lambda p: order.get(p.role, 3))
    ]
