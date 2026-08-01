import json
from pathlib import Path

import pytest

from utils import loader


ROOT = Path(__file__).parents[2]


def _valid_contract(*, chaos=None):
    chaos = chaos or ["Deathbringer", "Rod of Tahuti"]
    return {
        "schemaVersion": 2,
        "contractVersion": 2,
        "source": {"commit": "abc123"},
        "catalog": [*chaos, "Axe", "Health Potion"],
        "all": list(chaos),
        "pools": {"chaos": list(chaos)},
    }


def test_generated_primary_and_static_contracts_are_equivalent_and_safe():
    primary = json.loads((ROOT / "data" / "builds.json").read_text(encoding="utf-8"))
    fallback = json.loads(
        (ROOT / "utils" / "static_data" / "builds.json").read_text(encoding="utf-8")
    )

    assert primary == fallback
    assert primary["schemaVersion"] == primary["contractVersion"] == 2
    assert primary["all"] == primary["pools"]["chaos"]
    assert set(primary["pools"]["chaos"]) <= set(primary["catalog"])
    assert len(primary["pools"]["chaos"]) < len(primary["catalog"])
    for prohibited in (
        "Axe",
        "Health Potion",
        "Sentry Ward",
        "Agility Relic",
        "Alternator Mod",
        "Freya's Tears",
    ):
        assert prohibited in primary["catalog"]
        assert prohibited not in primary["pools"]["chaos"]
    for valid in ("Deathbringer", "Rod of Tahuti", "Gauntlet of Thebes"):
        assert valid in primary["pools"]["chaos"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda data: data["pools"].pop("chaos"), "explicit chaos"),
        (lambda data: data["pools"].update(chaos=[]), "explicit chaos"),
        (lambda data: data["pools"]["chaos"].append("Missing Item"), "catalog"),
        (
            lambda data: (
                data["pools"].update(chaos=list(data["catalog"])),
                data.update(all=list(data["catalog"])),
            ),
            "remain separate",
        ),
        (lambda data: data.update(all=["Axe"]), "compatibility"),
        (lambda data: data.pop("contractVersion"), "contract version"),
    ],
)
def test_invalid_contract_fails_safely_and_visibly(mutate, message):
    data = _valid_contract()
    mutate(data)

    with pytest.raises(ValueError, match=message):
        loader.validate_build_contract(data)


def test_loader_rejects_primary_static_drift_and_reload_clears_cache(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "data"
    static = tmp_path / "static"
    runtime.mkdir()
    static.mkdir()
    first = _valid_contract()
    primary_path = runtime / "builds.json"
    fallback_path = static / "builds.json"
    primary_path.write_text(json.dumps(first), encoding="utf-8")
    fallback_path.write_text(json.dumps(first), encoding="utf-8")
    monkeypatch.setattr(loader, "DATA_DIR", runtime)
    monkeypatch.setattr(loader, "STATIC_DATA_DIR", static)
    loader.reload()

    assert loader.builds()["source"]["commit"] == "abc123"
    changed = _valid_contract()
    changed["source"]["commit"] = "def456"
    primary_path.write_text(json.dumps(changed), encoding="utf-8")
    assert loader.builds()["source"]["commit"] == "abc123"

    loader.reload()
    with pytest.raises(ValueError, match="do not match"):
        loader.builds()

    fallback_path.write_text(json.dumps(changed), encoding="utf-8")
    loader.reload()
    assert loader.builds()["source"]["commit"] == "def456"
    loader.reload()
