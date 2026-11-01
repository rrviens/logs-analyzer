from app.specs.base import Maintenance, Spec

# Discipline heals through damage (Atonement), so it is judged on preparation, not on heal choice:
# how many Atonements are out when a big group hit lands (the shared "HoTs before hits" rules), whether the
# group is topped, and whether the ramp cooldowns (Evangelism, Ultimate Penitence) answer the damage.
# From 6 top +21-24 logs (Oct 2026): Evangelism ~every 2 min, Ultimate Penitence ~every 6 min; Atonement
# (buff 194384) is refreshed constantly (733 refreshes in one run), so early refreshes are never "wasted"
# (pandemic=1.0 switches that check off). No single-vs-group heal pair: almost no direct healing casts.
SPEC = Spec(
    key="discipline",
    class_name="Priest",
    spec_name="Discipline",
    label="Discipline Priest",
    plural="Discipline Priests",
    cooldowns={
        472433: ("Evangelism", 90, "healing"),
        421453: ("Ultimate Penitence", 240, "healing"),
        33206: ("Pain Suppression", 180, "utility"),
        32375: ("Mass Dispel", 120, "utility"),
        19236: ("Desperate Prayer", 90, "personal"),
    },
    maintenance=Maintenance(buff_id=194384, name="Atonement", plural="Atonements", duration_s=15.0, pandemic=1.0),
    tracked_buffs=("Weal and Woe", "Borrowed Time", "Power of the Dark Side"),
    mana_spells="Power Word: Radiance and Power Word: Shield",
    personal_habit="press Desperate Prayer about every 3 minutes",
    # Ramps are pressed on a plan, not into every dip: a trial on a top +23 log flagged Ultimate Penitence three times.
    ramp_cooldowns=("Evangelism", "Ultimate Penitence"),
    # One hit per cast (Penance fires several bolts, which would skew the per-cast average). No AoE button.
    dps_st=("Smite", "Mind Blast"),
)
