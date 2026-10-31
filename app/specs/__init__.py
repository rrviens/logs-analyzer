"""Healer specs the analyzer knows. Add a module with a SPEC and list it here."""
from app.specs.base import RACIALS, Spec
from app.specs.resto_shaman import SPEC as RESTO_SHAMAN

SPECS: list[Spec] = [RESTO_SHAMAN]


def spec_for(class_name: str, spec_name: str | None) -> Spec | None:
    """class_name may be WCL's ('DeathKnight') or spaced ('Death Knight')."""
    flat = class_name.replace(" ", "")
    return next((s for s in SPECS if s.class_name.replace(" ", "") == flat and s.spec_name.replace(" ", "")
                 == (spec_name or "").replace(" ", "")), None)


def healers_in(players) -> list[tuple]:
    """(player, spec) for every healer in the group whose spec is supported."""
    return [(p, s) for p in players if (s := spec_for(p.class_name, p.spec))]
