"""
Temporary guild settings persistence for the GodForge web dashboard.

This is a JSON bridge for the live Railway milestone. It is intentionally small
and replaceable once Discord OAuth, guild permissions, and a real database land.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from utils import dashboard_store

SETTINGS_PATH = Path("data/guild_settings.json")
DEFAULT_GUILD_ID = "global"

FEATURE_KEYS = ("botEnabled", "randomizerEnabled", "draftsEnabled", "bettingEnabled")
CHANNEL_KEYS = ("matchChannel", "bettingChannel", "adminChannel")
ROLE_KEYS = ("adminRole", "captainRole")
PERMISSION_KEYS = ("monetizeAccess",)
TEXT_FIELDS = CHANNEL_KEYS + ROLE_KEYS
MANAGED_RESOURCE_KEYS = (
    "playChannelId",
    "playMessageId",
    "roomCategoryId",
    "rolePanelChannelId",
    "rolePanelMessageId",
)
MANAGED_ROLE_KEYS = (
    "jungle",
    "mid",
    "adc",
    "support",
    "solo",
    "captain",
    "substitute",
    "lfg",
)
_settings_lock = threading.RLock()


def default_settings(guild_id: str = DEFAULT_GUILD_ID) -> dict:
    return {
        "guild_id": str(guild_id or DEFAULT_GUILD_ID),
        "features": {
            "botEnabled": True,
            "randomizerEnabled": True,
            "draftsEnabled": True,
            "bettingEnabled": True,
        },
        "channels": {
            "matchChannel": "",
            "bettingChannel": "",
            "adminChannel": "",
        },
        "roles": {
            "adminRole": "",
            "captainRole": "",
        },
        "permissions": {
            "monetizeAccess": "none",
        },
        "managed": {
            **{key: "" for key in MANAGED_RESOURCE_KEYS},
            "roleIds": {key: "" for key in MANAGED_ROLE_KEYS},
            "testMode": False,
        },
        "updated_at": None,
        "updated_by": None,
    }


def load_settings() -> dict:
    stored = dashboard_store.load_document("settings", "guilds", None)
    if stored is not None:
        return stored if isinstance(stored.get("guilds"), dict) else {"guilds": {}}

    if not SETTINGS_PATH.exists():
        return {"guilds": {}}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"guilds": {}}
    return data if isinstance(data.get("guilds"), dict) else {"guilds": {}}


def save_settings(data: dict):
    if dashboard_store.save_document("settings", "guilds", data):
        return

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_guild_settings(guild_id: str = DEFAULT_GUILD_ID) -> dict:
    gid = _clean_guild_id(guild_id)
    data = load_settings()
    saved = data.get("guilds", {}).get(gid, {})
    settings = default_settings(gid)
    settings["features"].update(_dict(saved.get("features")))
    settings["channels"].update(_dict(saved.get("channels")))
    settings["roles"].update(_dict(saved.get("roles")))
    settings["permissions"].update(_dict(saved.get("permissions")))
    settings["managed"].update(
        {
            key: value
            for key, value in _dict(saved.get("managed")).items()
            if key in MANAGED_RESOURCE_KEYS
        }
    )
    settings["managed"]["roleIds"].update(
        {
            key: value
            for key, value in _dict(_dict(saved.get("managed")).get("roleIds")).items()
            if key in MANAGED_ROLE_KEYS
        }
    )
    settings["managed"]["testMode"] = bool(
        _dict(saved.get("managed")).get("testMode", False)
    )
    settings["updated_at"] = saved.get("updated_at")
    settings["updated_by"] = saved.get("updated_by")
    return settings


def update_guild_settings(guild_id: str, payload: dict, updated_by: str | None = None) -> dict:
    with _settings_lock:
        return _update_guild_settings(guild_id, payload, updated_by)


def _update_guild_settings(
    guild_id: str,
    payload: dict,
    updated_by: str | None = None,
) -> dict:
    gid = _clean_guild_id(guild_id)
    current = get_guild_settings(gid)

    for key in FEATURE_KEYS:
        if key in _dict(payload.get("features")):
            current["features"][key] = bool(payload["features"][key])

    for key in CHANNEL_KEYS:
        if key in _dict(payload.get("channels")):
            current["channels"][key] = _clean_label(payload["channels"][key], key)

    for key in ROLE_KEYS:
        if key in _dict(payload.get("roles")):
            current["roles"][key] = _clean_label(payload["roles"][key], key)

    for key in PERMISSION_KEYS:
        if key in _dict(payload.get("permissions")):
            current["permissions"][key] = _clean_choice(
                payload["permissions"][key],
                {"none", "read", "manage"},
                key,
            )

    managed = _dict(payload.get("managed"))
    for key in MANAGED_RESOURCE_KEYS:
        if key in managed:
            current["managed"][key] = _clean_discord_id(managed[key], key)
    role_ids = _dict(managed.get("roleIds"))
    for key in MANAGED_ROLE_KEYS:
        if key in role_ids:
            current["managed"]["roleIds"][key] = _clean_discord_id(
                role_ids[key],
                f"roleIds.{key}",
            )
    if "testMode" in managed:
        current["managed"]["testMode"] = bool(managed["testMode"])

    current["updated_at"] = int(time.time())
    current["updated_by"] = _clean_label(updated_by or payload.get("updated_by") or "web-admin", "updated_by")

    data = load_settings()
    data.setdefault("guilds", {})[gid] = current
    save_settings(data)
    return current


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _clean_guild_id(value: str | None) -> str:
    gid = str(value or DEFAULT_GUILD_ID).strip() or DEFAULT_GUILD_ID
    if len(gid) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", gid):
        raise ValueError("Invalid guild id")
    return gid


def _clean_label(value, field_name: str) -> str:
    label = str(value or "").strip()
    if len(label) > 80:
        raise ValueError(f"{field_name} must be 80 characters or fewer")
    if any(ord(char) < 32 for char in label):
        raise ValueError(f"{field_name} cannot contain control characters")
    return label


def _clean_choice(value, choices: set[str], field_name: str) -> str:
    choice = str(value or "").strip().lower()
    if choice not in choices:
        raise ValueError(f"Invalid {field_name}")
    return choice


def _clean_discord_id(value, field_name: str) -> str:
    discord_id = str(value or "").strip()
    if discord_id and (not discord_id.isdigit() or len(discord_id) > 20):
        raise ValueError(f"Invalid {field_name}")
    return discord_id
