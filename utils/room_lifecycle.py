"""Temporary-room reconciliation and cleanup as a shared lifecycle hook.

Feature module for the Issue #48 refactor. Wraps the per-guild temporary-room
reconciliation (on startup) and cleanup (periodically) that previously lived as
inline loops in ``bot.py``'s ``on_ready`` and cleanup task, registering them
through the shared ``FeatureRegistry`` instead.

Domain logic stays in ``utils/match_rooms.py``; this module only sequences
per-guild service calls using the injected match-room repository, room-service
factory, and party repository (needed to check whether a lobby has reached a
terminal state before closing its rooms).
"""

from __future__ import annotations

import logging

from utils.lifecycle import LifecycleContext

log = logging.getLogger("godforge.room_lifecycle")


class RoomLifecycle:
    """Registers per-guild temporary-room reconciliation and cleanup."""

    name = "rooms"

    def __init__(self, match_room_repository, party_repository, service_factory):
        self._match_room_repository = match_room_repository
        self._party_repository = party_repository
        self._service_factory = service_factory

    async def on_startup(self, ctx: LifecycleContext) -> None:
        for guild in ctx.guilds:
            guild_rooms = tuple(self._match_room_repository.active(guild.id))
            if not guild_rooms:
                continue
            try:
                room_service = self._service_factory.for_guild(guild)
                for rooms in guild_rooms:
                    await room_service.reconcile(rooms.lobby_id)
            except Exception:
                log.exception(
                    "Temporary-room reconciliation failed for guild %s", guild.id
                )

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        for guild in ctx.guilds:
            guild_rooms = tuple(self._match_room_repository.active(guild.id))
            if not guild_rooms:
                continue
            try:
                room_service = self._service_factory.for_guild(guild)
                for rooms in guild_rooms:
                    lobby = self._party_repository.get(guild.id, rooms.lobby_id)
                    if lobby and lobby.is_terminal:
                        await room_service.close(
                            rooms.lobby_id,
                            reason=f"lobby {lobby.state.value}",
                            outcome=lobby.state.value,
                        )
                await room_service.cleanup_due()
            except Exception:
                log.exception("Temporary-room cleanup failed for guild %s", guild.id)
