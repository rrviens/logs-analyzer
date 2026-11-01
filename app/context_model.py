"""Loads the study output (data/context_models/<spec>.json) and matches it to a run's pulls."""
import json
from pathlib import Path

from app.config import DATA_DIR, PROJECT_ROOT
from app.runs import Pull
from app.specs.base import Spec


def _hosted_dir() -> Path | None:
    """Where a study refreshed on the hosted site is written (the persistent disk), or None when running locally."""
    return None if DATA_DIR.resolve() == PROJECT_ROOT.resolve() else DATA_DIR / "context_models"


def model_path(spec: Spec) -> Path:
    """The study to read: the data-disk copy when it is newer than the one in the repo."""
    repo = PROJECT_ROOT / "data" / "context_models" / f"{spec.key}.json"
    disk = _hosted_dir() / f"{spec.key}.json" if _hosted_dir() else None
    if disk and disk.exists() and (not repo.exists() or _generated(disk) >= _generated(repo)):
        return disk
    return repo


def write_model_path(spec: Spec) -> Path:
    """Where a fresh study goes: the data disk when hosted (survives deploys), else the repo."""
    return (_hosted_dir() or PROJECT_ROOT / "data" / "context_models") / f"{spec.key}.json"


def _generated(path: Path) -> str:
    try:
        return json.loads(path.read_text()).get("generated", "")
    except (OSError, ValueError):
        return ""


def doc_path(spec: Spec) -> Path:
    return (_hosted_dir().parent / "docs" if _hosted_dir() else PROJECT_ROOT / "docs" / "context_models") / f"{spec.key}.md"


LOOSE_OVERLAP = 0.34  # enemies shared (Jaccard) for a pack to lend its top-player band to an unmatched pull


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

    def band_segment(self, pull: Pull) -> tuple[dict | None, bool]:
        """(segment, approximate) for the Top players HP band. Exact match first; trash without one borrows the band of
        the studied pack sharing the most enemies (LOOSE_OVERLAP+). Findings keep using segment() (exact only)."""
        seg = self.segment(pull)
        if seg and seg.get("hp_curve"):
            return seg, False
        if pull.is_boss or not pull.npc_ids:
            return None, False
        mine = set(pull.npc_ids)
        scored = [(len(mine & set(s["npc_ids"])) / len(mine | set(s["npc_ids"])), s) for s in self.data["segments"]
                  if not s["encounter_id"] and s.get("hp_curve") and s["npc_ids"]]
        best = max(scored, key=lambda x: x[0], default=(0, None))
        return (best[1], True) if best[0] >= LOOSE_OVERLAP else (None, False)

    def runs_label(self) -> str:
        return ", ".join(f"+{r['key']}" for r in self.data["runs"])


def load_dungeon(name: str, spec: Spec) -> DungeonModel | None:
    path = model_path(spec)
    if not path.exists():
        return None
    d = json.loads(path.read_text()).get("dungeons", {}).get(name)
    return DungeonModel(d) if d else None
