"""Factory for per-guild temporary-room services.

Infrastructure module for the Issue #48 refactor. Building a ``MatchRoomService``
requires per-guild managed settings (room category + archive channel) and a grace
period that depends on test mode. This factory centralizes that wiring so
``bot.py`` (and, later, the party feature) can ask for a service by guild without
repeating the settings lookup.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from utils.discord_match_rooms import DiscordMatchRoomOperations
from utils.match_rooms import MatchRoomService

DEFAULT_GRACE_MINUTES = 10
TEST_MODE_GRACE_MINUTES = 1


class MatchRoomServiceFactory:
    """Builds a ``MatchRoomService`` for a guild from its managed settings."""

    def __init__(self, repository, settings_provider: Callable[[str], dict]):
        self._repository = repository
        self._settings_provider = settings_provider

    def for_guild(self, guild) -> MatchRoomService:
        managed = self._settings_provider(str(guild.id))["managed"]
        category_id = int(managed.get("roomCategoryId") or 0)
        if not category_id:
            raise RuntimeError("Run /party setup before creating temporary rooms.")
        grace = timedelta(
            minutes=TEST_MODE_GRACE_MINUTES
            if managed.get("testMode")
            else DEFAULT_GRACE_MINUTES
        )
        return MatchRoomService(
            self._repository,
            DiscordMatchRoomOperations(
                guild,
                category_id=category_id,
            ),
            empty_grace=grace,
        )
