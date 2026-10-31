"""Loads the study output (data/context_model.json) and matches it to a run's pulls."""
import json

from app.config import PROJECT_ROOT
from app.runs import Pull

MODEL_PATH = PROJECT_ROOT / "data" / "context_model.json"


class DungeonModel:
    def __init__(self, data: dict):
        self.data = data
        self.casts_per_min: dict[str, float] = data.get("casts_per_min", {})

    def segment(self, pull: Pull) -> dict | None:
        """Bosses match by encounter. Trash matches only a studied pack with the same
        lead mob - enemy sets overlap too much across packs to trust them alone."""
        if pull.is_boss:
            return next((s for s in self.data["segments"] if s["encounter_id"] == pull.encounter_id), None)
        same_name = [s for s in self.data["segments"] if not s["encounter_id"] and s["name"] == pull.name]
        if not same_name:
            return None

        def overlap(s):
            a, b = set(pull.npc_ids), set(s["npc_ids"])
            return len(a & b) / len(a | b) if a and b else 0.0
        return max(same_name, key=overlap)

    def runs_label(self) -> str:
        return ", ".join(f"+{r['key']}" for r in self.data["runs"])


def load_dungeon(name: str) -> DungeonModel | None:
    if not MODEL_PATH.exists():
        return None
    d = json.loads(MODEL_PATH.read_text()).get("dungeons", {}).get(name)
    return DungeonModel(d) if d else None
