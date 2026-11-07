"""Stored heals (Stormstream Totem) in held-cooldown findings: used on the moment = reasonable hold, kept unused =
flagged too, absent = nothing. Run: .venv/Scripts/python.exe -m tests.test_stored"""
from types import SimpleNamespace as NS

from app.findings import _stored_note
from app.healer_state import HealerState


def ctx(spans):
    state = HealerState(stored={"Stormstream Totem": spans})
    return NS(state=state, run=NS(end=10**9), t_of=lambda b: b * 1000, clock=lambda t: f"{t // 1000}s")


def main() -> None:
    w = NS(start=100, end=105)  # bins = seconds: danger from 100s to 106s
    line, cut = _stored_note(ctx([[90_000, 102_000]]), [w], "Healing Tide Totem")
    assert "used on it (at 102s)" in line and "holding Healing Tide Totem" in line and cut == 20, line
    line, cut = _stored_note(ctx([[95_000, 200_000]]), [w], "Healing Tide Totem")
    assert "stored and unused through it" in line and cut == 0, line
    assert _stored_note(ctx([[10_000, 20_000]]), [w], "Healing Tide Totem") is None
    two = [w, NS(start=300, end=305)]
    line, cut = _stored_note(ctx([[90_000, 102_000]]), two, "Healing Tide Totem")
    assert "1 of them" in line and cut == 10, line
    print("stored heal checks ok")


if __name__ == "__main__":
    main()
