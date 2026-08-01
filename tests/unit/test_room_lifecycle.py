"""RoomLifecycle startup/cleanup hooks (Issue #48, Phase 8c)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.lifecycle import LifecycleContext
from utils.room_lifecycle import RoomLifecycle


def _guild(guild_id):
    return MagicMock(id=guild_id)


def _lobby(*, is_terminal=False, state="active"):
    lobby = MagicMock()
    lobby.is_terminal = is_terminal
    lobby.state = MagicMock(value=state)
    return lobby


def test_name_is_rooms():
    lifecycle = RoomLifecycle(MagicMock(), MagicMock(), MagicMock())
    assert lifecycle.name == "rooms"


async def test_startup_skips_guilds_with_no_active_rooms():
    match_room_repo = MagicMock()
    match_room_repo.active.return_value = ()
    factory = MagicMock()
    lifecycle = RoomLifecycle(match_room_repo, MagicMock(), factory)

    ctx = LifecycleContext(get_guild=lambda gid: None, guilds=(_guild(1),))
    await lifecycle.on_startup(ctx)

    factory.for_guild.assert_not_called()


async def test_startup_reconciles_active_rooms():
    match_room_repo = MagicMock()
    rooms = MagicMock(lobby_id="lobby-1")
    match_room_repo.active.return_value = (rooms,)
    room_service = MagicMock()
    room_service.reconcile = AsyncMock()
    factory = MagicMock()
    factory.for_guild.return_value = room_service
    lifecycle = RoomLifecycle(match_room_repo, MagicMock(), factory)

    ctx = LifecycleContext(get_guild=lambda gid: None, guilds=(_guild(1),))
    await lifecycle.on_startup(ctx)

    room_service.reconcile.assert_awaited_once_with("lobby-1")


async def test_startup_isolates_per_guild_failure():
    match_room_repo = MagicMock()
    match_room_repo.active.return_value = (MagicMock(lobby_id="lobby-1"),)
    factory = MagicMock()
    factory.for_guild.side_effect = RuntimeError("boom")
    lifecycle = RoomLifecycle(match_room_repo, MagicMock(), factory)

    ctx = LifecycleContext(get_guild=lambda gid: None, guilds=(_guild(1), _guild(2)))
    # Must not raise even though guild 1's factory lookup fails.
    await lifecycle.on_startup(ctx)
    assert factory.for_guild.call_count == 2


async def test_cleanup_closes_terminal_lobby_rooms_and_calls_cleanup_due():
    match_room_repo = MagicMock()
    rooms = MagicMock(lobby_id="lobby-1")
    match_room_repo.active.return_value = (rooms,)
    party_repo = MagicMock()
    party_repo.get.return_value = _lobby(is_terminal=True, state="cancelled")
    room_service = MagicMock()
    room_service.close = AsyncMock()
    room_service.cleanup_due = AsyncMock()
    factory = MagicMock()
    factory.for_guild.return_value = room_service
    lifecycle = RoomLifecycle(match_room_repo, party_repo, factory)

    ctx = LifecycleContext(get_guild=lambda gid: None, guilds=(_guild(1),))
    await lifecycle.on_cleanup(ctx)

    room_service.close.assert_awaited_once_with(
        "lobby-1", reason="lobby cancelled", outcome="cancelled"
    )
    room_service.cleanup_due.assert_awaited_once()


async def test_cleanup_does_not_close_non_terminal_lobby():
    match_room_repo = MagicMock()
    rooms = MagicMock(lobby_id="lobby-1")
    match_room_repo.active.return_value = (rooms,)
    party_repo = MagicMock()
    party_repo.get.return_value = _lobby(is_terminal=False)
    room_service = MagicMock()
    room_service.close = AsyncMock()
    room_service.cleanup_due = AsyncMock()
    factory = MagicMock()
    factory.for_guild.return_value = room_service
    lifecycle = RoomLifecycle(match_room_repo, party_repo, factory)

    ctx = LifecycleContext(get_guild=lambda gid: None, guilds=(_guild(1),))
    await lifecycle.on_cleanup(ctx)

    room_service.close.assert_not_awaited()
    room_service.cleanup_due.assert_awaited_once()
