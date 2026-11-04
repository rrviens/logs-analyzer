"""Cooldowns with charges (Survival of the Fittest with its talent, Riptide...): one charge left means the spell is
still ready. Charges and the recharge time are learned per player from their own casts, because talents add charges
and cut cooldowns in ways the base numbers don't show."""

QUICK_SHARE = 0.5  # two casts closer than half the base cooldown: the player has a second charge
SAME_USE_MS = 2000  # casts this close are one use (channel ticks, a recast that ends the effect)


def uses(times: list[int], within_ms: float = SAME_USE_MS) -> list[int]:
    """One entry per use: casts inside `within_ms` of a use's first cast belong to it (Alter Time's recast,
    Tranquility's ticks), so they don't look like a second charge."""
    out = []
    for x in sorted(times):
        if not out or x - out[-1] >= within_ms:
            out.append(x)
    return out


def infer_charges(times: list[int], base_ms: float) -> int:
    """Most casts seen inside any window shorter than QUICK_SHARE of the base cooldown (at least 1)."""
    times, best, j = sorted(times), 1, 0
    for i, x in enumerate(times):
        while x - times[j] >= base_ms * QUICK_SHARE:
            j += 1
        best = max(best, i - j + 1)
    return best


def learn_recharge_ms(times: list[int], base_s: float, charges: int = 1) -> float:
    """Base cooldown, or shorter when the log proves it (talents/CDR): with N charges, N+1 casts need at least one
    full recharge between the first and the last."""
    times = sorted(times)
    return min([base_s * 1000] + [b - a for a, b in zip(times, times[charges:])])


def wait_ms(times: list[int], t: int, recharge_ms: float, charges: int = 1) -> float:
    """How long after t until a charge is free (0 = ready at t). Charges recharge one at a time, like in game."""
    have, done = charges, None  # charges in hand, when the running recharge finishes

    def recharge_until(x):
        nonlocal have, done
        while done is not None and done <= x:
            have += 1
            done = done + recharge_ms if have < charges else None

    for x in sorted(c for c in times if c <= t):
        recharge_until(x)
        have = max(0, have - 1)  # the log says they cast it: never below zero even if our numbers are off
        if done is None:
            done = x + recharge_ms
    recharge_until(t)
    return 0.0 if have >= 1 else done - t


def free_at(times: list[int], t: int, base_s: float) -> int:
    """When a charge of this player's spell is free at or after t (t itself = ready), charges and recharge learned."""
    charges = infer_charges(times, base_s * 1000)
    return int(t + wait_ms(times, t, learn_recharge_ms(times, base_s, charges), charges))


assert uses([0, 500, 9_000, 30_000], 10_000) == [0, 30_000]
assert infer_charges([0, 5_000, 200_000], 180_000) == 2 and infer_charges([0, 100_000], 180_000) == 1
assert wait_ms([0, 5_000], 10_000, 90_000, 2) == 80_000  # both charges spent; the first comes back at 90s
assert wait_ms([0], 10_000, 90_000, 2) == 0  # one charge left
assert wait_ms([0], 10_000, 90_000, 1) == 80_000
assert wait_ms([10_000], 10_000, 90_000, 1) == 90_000  # cast exactly at t counts once
assert learn_recharge_ms([0, 5_000, 95_000], 180, 2) == 95_000
