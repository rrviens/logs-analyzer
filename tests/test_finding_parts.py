"""Pins the line shapes app/finding_parts.py sorts on (real lines from saved analyses).
Run: .venv/Scripts/python.exe -m tests.test_finding_parts"""
from app.finding_parts import structure


def main() -> None:
    s = structure([
        "Earthbound's Imprint: 58% of the damage in the last 10s",
        "Frost Overload: 42% of the damage in the last 10s",
        "Earthbound's Imprint was on Waràk from 4:29 - 2.0s until the death.",
        "Peer Resto Shamans with Earthbound's Imprint on themselves (50 times in 8 runs) got rid of it after 8.0s median.",
        "Also: Not topped before Frost Overload (25x).",
        "Also hit Pmon: it dropped off with no dispel or known escape seen after 3s.",
        "Also: Not topped before Frost Overload (25x).",
        "  8 of the 25 Frost Overload hits left someone below 20% HP.",
        "Your group averaged 74% before it; peer groups hold 87%.",
        "Top Resto Shamans go in with 3 Riptides out (you: 2.8).",
        "Healing pressure here is partly damage peer groups avoid or mitigate. Biggest differences:",
        "  Cinderbolt: 4.1% group HP/s vs 2.9% - 3 casts went through that peer groups usually interrupt or stun",
        "  Sharp Nail: 1.2% group HP/s vs 0.8%",
        "All pulls:",
        "  Pull 3: Keen Taskmaster (2:10): 40% more",
        "  Melee: 30% of it landed on DPS or healer - mobs not held by the tank.",
        "Not used again this run.",
        "Pull 4: Queen Patlaa: not pressed - no top-run data for this pack; sat ready through 40s of pressure.",
    ], [["Ready?", "Ready and would likely have saved them: Feint."], ["Top players", "Press it here 5 of 8 runs."]])
    assert s["shares"] == {"seconds": 10, "rows": [["Earthbound's Imprint", 58], ["Frost Overload", 42]]}, s["shares"]
    assert s["rates"] == [["Cinderbolt", 4.1, 2.9, "3 casts went through that peer groups usually interrupt or stun"],
                          ["Sharp Nail", 1.2, 0.8, ""]], s["rates"]
    assert [w["text"][:12] for w in s["why"]] == ["Earthbound's", "All pulls:", "Not used aga"], s["why"]
    assert s["why"][1]["sub"] == ["Pull 3: Keen Taskmaster (2:10): 40% more",
                                  "Melee: 30% of it landed on DPS or healer - mobs not held by the tank."]
    assert [p["text"][:10] for p in s["peers"]] == ["Peer Resto", "Your group", "Top Resto ", "Pull 4: Qu"], s["peers"]
    assert len(s["notes"]) == 3 and s["notes"][2]["sub"] == ["8 of the 25 Frost Overload hits left someone below 20% HP."] and s["rates_title"] == "Healing pressure here is partly damage peer groups avoid or mitigate."
    assert s["verdict"] == [["Ready?", "Ready and would likely have saved them: Feint."]]
    assert s["peer_context"] == [["Top players", "Press it here 5 of 8 runs."]]
    print("finding parts ok")


if __name__ == "__main__":
    main()
