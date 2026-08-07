import random

import pytest

from utils.picker import pick_build


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
