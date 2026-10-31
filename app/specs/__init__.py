"""Healer specs the analyzer knows. Add a module with a SPEC and list it here."""
from app.specs.base import RACIALS, Spec
from app.specs.holy_paladin import SPEC as HOLY_PALADIN
from app.specs.holy_priest import SPEC as HOLY_PRIEST
from app.specs.mistweaver import SPEC as MISTWEAVER
from app.specs.preservation import SPEC as PRESERVATION
from app.specs.resto_druid import SPEC as RESTO_DRUID
from app.specs.resto_shaman import SPEC as RESTO_SHAMAN

SPECS: list[Spec] = [RESTO_SHAMAN, RESTO_DRUID, MISTWEAVER, HOLY_PRIEST, PRESERVATION, HOLY_PALADIN]


def spec_for(class_name: str, spec_name: str | None) -> Spec | None:
    """class_name may be WCL's ('DeathKnight') or spaced ('Death Knight')."""
    flat = class_name.replace(" ", "")
    return next((s for s in SPECS if s.class_name.replace(" ", "") == flat and s.spec_name.replace(" ", "")
                 == (spec_name or "").replace(" ", "")), None)


def healers_in(players) -> list[tuple]:
    """(player, spec) for every healer in the group whose spec is supported."""
    return [(p, s) for p in players if p.role == "healer" and (s := spec_for(p.class_name, p.spec))]
