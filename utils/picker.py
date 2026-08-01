"""
Random selection logic.

`pick_god` returns a single god name from the appropriate pool.
`pick_team` returns 5 unique god names from the appropriate pool.
`pick_build` returns N unique items from the appropriate pool (default 6).

All functions raise ValueError with a user-friendly message on bad data
(empty pool, missing key, pool too small for request, etc.). The caller
is responsible for catching and surfacing those to the user.
"""

import random

DEFAULT_BUILD_SIZE = 6
TEAM_SIZE = 5


def pick_god(gods_data: dict, role: str | None, source: str,
             exclude: set | None = None) -> str:
    """
    Pick a random god.

    role:    "jungle" | "mid" | "adc" | "support" | "solo" | None (any)
    source:  "website" | "tab"
    exclude: set of god names to exclude (used during active sessions)

    When role is None, picks from the flat 'all' roster (source ignored).
    When role is set, picks from gods_data['pools'][source][role].
    """
    if role is None:
        candidates = list(set(gods_data.get("all", [])))
        if not candidates:
            raise ValueError("Full god roster ('all') is empty.")
    else:
        pools = gods_data.get("pools", {})
        if source not in pools:
            raise ValueError(f"Unknown god pool source: '{source}'.")
        if role not in pools[source]:
            raise ValueError(f"No '{role}' pool found in '{source}' source.")
        candidates = list(set(pools[source][role]))

    if exclude:
        candidates = [g for g in candidates if g not in exclude]

    if not candidates:
        scope = f"{source}/{role}" if role else "all"
        raise ValueError(f"No unpicked gods remaining in '{scope}'.")

    return random.choice(candidates)


def _get_god_weight(god: str, gods_data: dict) -> float:
    """
    Return the weight for a god based on which role pools it appears in.
    Uses the highest weight among all role memberships across both sources.
    Falls back to the 'default' weight if the god isn't in any role pool.
    """
    weights_config = gods_data.get("weights", {})
    default_weight = weights_config.get("default", 1.0)

    best_weight = None
    pools = gods_data.get("pools", {})
    for source_pools in pools.values():
        if isinstance(source_pools, dict):
            for role, role_gods in source_pools.items():
                if role.startswith("_"):
                    continue
                if god in role_gods:
                    w = weights_config.get(role, default_weight)
                    if best_weight is None or w > best_weight:
                        best_weight = w

    return best_weight if best_weight is not None else default_weight


def pick_team(gods_data: dict, role: str | None, source: str,
              exclude: set | None = None) -> list[str]:
    """
    Pick 5 unique gods. Same role/source semantics as pick_god.

    When role is None (.roll5 from full roster), uses role-based weights
    from gods_data['weights'] so support/solo gods appear more often.
    Weights are recalculated on the remaining pool after exclusions.
    When role is specified, picks uniformly (weighting within a single
    role is always equal).
    """
    if role is None:
        candidates = list(set(gods_data.get("all", [])))
        scope = "all"

        if exclude:
            candidates = [g for g in candidates if g not in exclude]

        if len(candidates) < TEAM_SIZE:
            raise ValueError(
                f"Not enough unpicked gods remaining in '{scope}' "
                f"({len(candidates)} left, need {TEAM_SIZE})."
            )

        # Weighted selection without replacement on remaining pool
        weights = [_get_god_weight(g, gods_data) for g in candidates]

        # Safety: if all weights are 0 or invalid, fall back to uniform
        if not any(w > 0 for w in weights):
            weights = [1.0] * len(candidates)

        selected = []
        remaining = list(range(len(candidates)))
        remaining_weights = list(weights)

        for _ in range(TEAM_SIZE):
            chosen_idx = random.choices(remaining, weights=remaining_weights, k=1)[0]
            pos = remaining.index(chosen_idx)
            selected.append(candidates[chosen_idx])
            remaining.pop(pos)
            remaining_weights.pop(pos)

        return selected
    else:
        pools = gods_data.get("pools", {})
        if source not in pools:
            raise ValueError(f"Unknown god pool source: '{source}'.")
        if role not in pools[source]:
            raise ValueError(f"No '{role}' pool found in '{source}' source.")
        candidates = list(set(pools[source][role]))
        scope = f"{source}/{role}"

        if exclude:
            candidates = [g for g in candidates if g not in exclude]

        if len(candidates) < TEAM_SIZE:
            raise ValueError(
                f"Not enough unpicked gods remaining in '{scope}' "
                f"({len(candidates)} left, need {TEAM_SIZE})."
            )
        return random.sample(candidates, TEAM_SIZE)


def pick_build(builds_data: dict, role: str, build_type: str | None,
               count: int = DEFAULT_BUILD_SIZE) -> list[str]:
    """
    Pick `count` unique items for a build (default 6).

    role:       "chaos" -> from explicit pools.chaos, type ignored
                "support" -> from pools.support (no type)
                "adc" -> from pools.adc[type]   (type: standard|str|hyb)
                "mid"/"jungle"/"solo" -> from pools[role][type]
    """
    if role == "chaos":
        pool = builds_data.get("pools", {}).get("chaos", [])
        if not pool:
            raise ValueError(
                "The explicit chaos build pool is missing or empty. "
                "Regenerate the GodForge content contract before using `.rc`."
            )
        scope = "chaos"
    else:
        pools = builds_data.get("pools", {})
        if role == "support":
            pool = pools.get("support", [])
            scope = "support"
        elif role == "adc":
            adc_pools = pools.get("adc", {})
            if build_type not in adc_pools:
                raise ValueError(f"No ADC variant '{build_type}' in builds.")
            pool = adc_pools[build_type]
            scope = f"adc/{build_type}"
        else:
            role_pools = pools.get(role, {})
            if build_type not in role_pools:
                raise ValueError(f"No build type '{build_type}' for role '{role}'.")
            pool = role_pools[build_type]
            scope = f"{role}/{build_type}"

    unique_items = list(dict.fromkeys(pool))
    if len(unique_items) < count:
        raise ValueError(
            f"Build pool '{scope}' has only {len(unique_items)} unique items "
            f"(need at least {count})."
        )

    return random.sample(unique_items, count)
