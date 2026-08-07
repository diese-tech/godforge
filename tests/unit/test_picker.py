import random

import pytest

from utils.picker import MAX_ACTIVE_ITEMS_IN_CHAOS_BUILD, pick_build


def _contract():
    return {
        "schemaVersion": 2,
        "contractVersion": 2,
        "catalog": ["Deathbringer", "Rod of Tahuti", "Axe", "Health Potion"],
        "all": ["Deathbringer", "Rod of Tahuti"],
        "pools": {"chaos": ["Deathbringer", "Rod of Tahuti"]},
    }


def test_chaos_samples_only_explicit_pool_and_never_catalog_or_all():
    data = _contract()
    data["all"] = ["Axe", "Health Potion"]

    assert set(pick_build(data, "chaos", None, count=2)) == {
        "Deathbringer",
        "Rod of Tahuti",
    }


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_chaos_preserves_unique_count_behavior(count):
    chaos = [f"Final Item {index}" for index in range(1, 7)]
    data = {"all": ["Axe"], "pools": {"chaos": chaos}}
    random.seed(count)

    picked = pick_build(data, "chaos", None, count=count)

    assert len(picked) == count
    assert len(set(picked)) == count
    assert set(picked) <= set(chaos)


def test_missing_or_empty_chaos_never_falls_back_to_all_catalog():
    for pools in ({}, {"chaos": []}):
        with pytest.raises(ValueError, match="explicit chaos"):
            pick_build(
                {"catalog": ["Axe"], "all": ["Axe"], "pools": pools},
                "chaos",
                None,
            )


def _contract_with_actives(num_active=43, num_inactive=97):
    active = [f"Active Item {i}" for i in range(num_active)]
    inactive = [f"Passive Item {i}" for i in range(num_inactive)]
    chaos = active + inactive
    return {
        "all": chaos,
        "pools": {"chaos": chaos},
        "activeItemNames": active,
    }, set(active), set(chaos)


def test_chaos_build_never_exceeds_active_item_cap_across_many_trials():
    data, active_names, chaos_pool = _contract_with_actives()

    for seed in range(1500):
        random.seed(seed)
        picked = pick_build(data, "chaos", None, count=6)

        assert len(picked) == 6
        assert len(set(picked)) == 6
        assert set(picked) <= chaos_pool

        num_active_picked = sum(1 for item in picked if item in active_names)
        assert num_active_picked <= MAX_ACTIVE_ITEMS_IN_CHAOS_BUILD


def test_chaos_build_can_still_hit_the_active_item_cap():
    # Sanity check that the cap is actually being exercised and not just
    # trivially satisfied because actives are rarely drawn.
    data, active_names, _ = _contract_with_actives()

    hit_cap = False
    for seed in range(500):
        random.seed(seed)
        picked = pick_build(data, "chaos", None, count=6)
        if sum(1 for item in picked if item in active_names) == MAX_ACTIVE_ITEMS_IN_CHAOS_BUILD:
            hit_cap = True
            break

    assert hit_cap


def test_chaos_build_raises_when_active_cap_makes_count_infeasible():
    # Only 2 non-active items exist, but count=6 needs at least 4 actives
    # to fill the build, which exceeds the 3-active cap.
    active = [f"Active Item {i}" for i in range(10)]
    inactive = ["Passive Item 0", "Passive Item 1"]
    data = {
        "all": active + inactive,
        "pools": {"chaos": active + inactive},
        "activeItemNames": active,
    }

    with pytest.raises(ValueError, match="active"):
        pick_build(data, "chaos", None, count=6)


def test_chaos_build_without_active_item_data_behaves_as_before():
    # Contract v2 data has no 'activeItemNames' key at all; the constraint
    # should be a no-op rather than an error.
    chaos = [f"Item {i}" for i in range(10)]
    data = {"all": chaos, "pools": {"chaos": chaos}}
    random.seed(0)

    picked = pick_build(data, "chaos", None, count=6)

    assert len(picked) == 6
    assert set(picked) <= set(chaos)
