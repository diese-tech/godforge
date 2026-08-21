"""Provision and project GodForge-managed cosmetic Discord roles.

Discord role IDs are the only resource identity accepted by this module. Role
names are presentation and are deliberately never used for reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import discord


@dataclass(frozen=True)
class ManagedRoleDefinition:
    key: str
    name: str
    optional: bool = False


DEFAULT_ROLE_DEFINITIONS = (
    ManagedRoleDefinition("solo", "GodForge • Solo"),
    ManagedRoleDefinition("jungle", "GodForge • Jungle"),
    ManagedRoleDefinition("mid", "GodForge • Mid"),
    ManagedRoleDefinition("support", "GodForge • Support"),
    ManagedRoleDefinition("adc", "GodForge • ADC"),
)

OPTIONAL_ROLE_DEFINITIONS = (
    ManagedRoleDefinition("captain", "GodForge • Captain", optional=True),
    ManagedRoleDefinition("substitute", "GodForge • Substitute", optional=True),
    ManagedRoleDefinition("lfg", "GodForge • LFG", optional=True),
)

ROLE_DEFINITIONS = DEFAULT_ROLE_DEFINITIONS + OPTIONAL_ROLE_DEFINITIONS


class ManagedRoleError(RuntimeError):
    """A safe, administrator-facing managed-role failure."""


class MissingManageRoles(ManagedRoleError):
    pass


class InvalidRoleHierarchy(ManagedRoleError):
    pass


class UnknownManagedRole(ManagedRoleError):
    pass


class ManagedRoleMissing(ManagedRoleError):
    pass


@dataclass(frozen=True)
class ReconcileResult:
    role_ids: dict[str, int]
    created_keys: tuple[str, ...]


def _definition_map(
    definitions: Iterable[ManagedRoleDefinition],
) -> dict[str, ManagedRoleDefinition]:
    return {definition.key: definition for definition in definitions}


def _bot_member(guild):
    member = getattr(guild, "me", None)
    if member is None:
        raise ManagedRoleError("GodForge could not resolve its guild member.")
    return member


def preflight(guild, roles: Iterable[object] = ()) -> None:
    """Verify that GodForge can create and assign every supplied role."""

    bot_member = _bot_member(guild)
    permissions = getattr(bot_member, "guild_permissions", None)
    if not permissions or not getattr(permissions, "manage_roles", False):
        raise MissingManageRoles(
            "GodForge needs the Manage Roles permission to manage cosmetic roles."
        )

    top_role = getattr(bot_member, "top_role", None)
    if top_role is None:
        raise InvalidRoleHierarchy("GodForge could not determine its highest role.")

    for role in roles:
        if role is None:
            continue
        if getattr(role, "is_default", lambda: False)():
            raise InvalidRoleHierarchy("GodForge cannot manage the @everyone role.")
        if getattr(role, "position", -1) >= getattr(top_role, "position", -1):
            raise InvalidRoleHierarchy(
                f"Move the GodForge bot role above {getattr(role, 'name', 'the managed role')}."
            )


async def reconcile(
    guild,
    stored_role_ids: Mapping[str, int | str | None],
    *,
    enabled_keys: Iterable[str] | None = None,
    definitions: Iterable[ManagedRoleDefinition] = ROLE_DEFINITIONS,
    reason: str = "GodForge managed cosmetic role",
) -> ReconcileResult:
    """Reuse stored role IDs and create only missing requested roles.

    A same-named role with a different ID is intentionally ignored. Callers
    should persist ``role_ids`` after this function returns.
    """

    by_key = _definition_map(definitions)
    requested = (
        tuple(enabled_keys)
        if enabled_keys is not None
        else tuple(definition.key for definition in definitions if not definition.optional)
    )
    unknown = [key for key in requested if key not in by_key]
    if unknown:
        raise UnknownManagedRole(f"Unknown GodForge role key: {unknown[0]}")

    existing: dict[str, object] = {}
    for key in requested:
        raw_id = stored_role_ids.get(key)
        if raw_id in (None, ""):
            continue
        try:
            role_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ManagedRoleMissing(f"Stored role ID for {key} is invalid.") from exc
        role = guild.get_role(role_id)
        if role is not None:
            existing[key] = role

    preflight(guild, existing.values())

    role_ids: dict[str, int] = {}
    created: list[str] = []
    for key in requested:
        role = existing.get(key)
        if role is None:
            definition = by_key[key]
            role = await guild.create_role(
                name=definition.name,
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=False,
                reason=reason,
            )
            preflight(guild, (role,))
            created.append(key)
        role_ids[key] = int(role.id)

    return ReconcileResult(role_ids=role_ids, created_keys=tuple(created))


async def set_member_role(
    guild,
    member,
    key: str,
    enabled: bool,
    stored_role_ids: Mapping[str, int | str | None],
    *,
    definitions: Iterable[ManagedRoleDefinition] = ROLE_DEFINITIONS,
    reason: str = "GodForge self-assigned cosmetic role",
) -> bool:
    """Idempotently add or remove one stored managed role.

    Returns ``True`` only when Discord membership was changed.
    """

    if key not in _definition_map(definitions):
        raise UnknownManagedRole(f"Unknown GodForge role key: {key}")
    raw_id = stored_role_ids.get(key)
    if raw_id in (None, ""):
        raise ManagedRoleMissing(f"No managed Discord role is stored for {key}.")
    try:
        role_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ManagedRoleMissing(f"Stored role ID for {key} is invalid.") from exc

    role = guild.get_role(role_id)
    if role is None:
        raise ManagedRoleMissing(
            f"The managed Discord role for {key} was deleted; run setup to recreate it."
        )
    preflight(guild, (role,))

    current_ids = {int(existing.id) for existing in getattr(member, "roles", ())}
    has_role = role_id in current_ids
    if enabled and not has_role:
        await member.add_roles(role, reason=reason)
        return True
    if not enabled and has_role:
        await member.remove_roles(role, reason=reason)
        return True
    return False
