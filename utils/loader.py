"""
Loads JSON data files. Cached in module-level variables; call `reload()`
to force re-read (useful if you edit the JSON files while the bot runs).
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
STATIC_DATA_DIR = Path(__file__).parent / "static_data"

_gods_cache = None
_builds_cache = None
_aliases_cache = None


def _load(filename: str) -> dict:
    path = DATA_DIR / filename
    if not path.exists():
        fallback = STATIC_DATA_DIR / filename
        if fallback.exists():
            path = fallback
        else:
            raise FileNotFoundError(f"Data file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_build_contract(data: dict) -> dict:
    """Fail closed when generated build eligibility is ambiguous or unsafe."""
    if data.get("schemaVersion") != 2:
        raise ValueError("GodForge build schema version 2 is required.")
    if data.get("contractVersion") != 2:
        raise ValueError("GodForge build contract version 2 is required.")
    source = data.get("source")
    if not isinstance(source, dict) or "commit" not in source:
        raise ValueError("GodForge build contract source metadata is missing.")
    catalog = data.get("catalog")
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("GodForge build catalog is missing or empty.")
    if len(set(catalog)) != len(catalog):
        raise ValueError("GodForge build catalog contains duplicate items.")
    chaos = data.get("pools", {}).get("chaos")
    if not isinstance(chaos, list) or not chaos:
        raise ValueError("GodForge explicit chaos pool is missing or empty.")
    if len(set(chaos)) != len(chaos):
        raise ValueError("GodForge explicit chaos pool contains duplicate items.")
    catalog_names = set(catalog)
    unresolved = [item for item in chaos if item not in catalog_names]
    if unresolved:
        raise ValueError(
            "GodForge chaos items do not resolve to the canonical catalog: "
            + ", ".join(unresolved)
        )
    if set(chaos) == catalog_names:
        raise ValueError(
            "GodForge chaos eligibility must remain separate from the full catalog."
        )
    if data.get("all") != chaos:
        raise ValueError(
            "GodForge compatibility `all` must match the explicit chaos pool."
        )
    return data


def _load_builds() -> dict:
    primary_path = DATA_DIR / "builds.json"
    fallback_path = STATIC_DATA_DIR / "builds.json"
    if not primary_path.exists() and not fallback_path.exists():
        raise FileNotFoundError(f"Data file not found: {primary_path}")

    primary = (
        validate_build_contract(_read_json(primary_path))
        if primary_path.exists()
        else None
    )
    fallback = (
        validate_build_contract(_read_json(fallback_path))
        if fallback_path.exists()
        else None
    )
    if primary is not None and fallback is not None and primary != fallback:
        raise ValueError(
            "Primary and static GodForge build contracts do not match. "
            "Regenerate both artifacts together."
        )
    return primary if primary is not None else fallback


def gods() -> dict:
    global _gods_cache
    if _gods_cache is None:
        _gods_cache = _load("gods.json")
    return _gods_cache


def builds() -> dict:
    global _builds_cache
    if _builds_cache is None:
        _builds_cache = _load_builds()
    return _builds_cache


def aliases() -> dict:
    """Load god name aliases. Keys starting with '_' are ignored (comments)."""
    global _aliases_cache
    if _aliases_cache is None:
        raw = _load("aliases.json")
        _aliases_cache = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
    return _aliases_cache


def reload():
    """Clear caches so next access re-reads from disk."""
    global _gods_cache, _builds_cache, _aliases_cache
    _gods_cache = None
    _builds_cache = None
    _aliases_cache = None
