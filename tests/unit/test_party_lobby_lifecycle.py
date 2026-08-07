"""PartyLobbyService.expire_ready_checks / PartyLobbyFeature (Issue #48, Phase 8e)."""

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from utils.lifecycle import LifecycleContext
from utils.party import DiscordDelivery, LobbyState, PartyLobby
from utils.party_lobby import PartyLobbyDeps, PartyLobbyFeature, PartyLobbyService
from utils.party_queue import QueueStatus


def _service(
    *, party_repository=None, party_queue_service=None, settings_module=None,
    match_room_service_for_guild=None,
):
    deps = PartyLobbyDeps(
        party_repository=party_repository or MagicMock(),
        party_queue_service=party_queue_service or MagicMock(),
        party_draft_repository=MagicMock(),
        scrim_repository=MagicMock(),
        drafts=MagicMock(),
        draft_coordinator=MagicMock(),
        activity_client=MagicMock(),
        formatter=MagicMock(),
        settings_module=settings_module or MagicMock(),
        reserve_match_id=lambda: "GF-1",
        match_room_service_for_guild=(
            match_room_service_for_guild or (lambda guild: MagicMock())
        ),
        channel_has_active=lambda cid: None,
        save_active_draft=lambda cid, did: None,
        ensure_match_history=lambda lobby, launch: None,
        match_result_embed=lambda record: MagicMock(),
        set_member_role=AsyncMock(),
        log=MagicMock(),
        lobby_card_view=lambda: MagicMock(),
        ready_check_view=lambda: MagicMock(),
        role_preferences_view=lambda: MagicMock(),
        create_lobby_view=lambda handler, **kwargs: MagicMock(),
        join_preferences_view=lambda handler, **kwargs: MagicMock(),
        match_result_view=lambda: MagicMock(),
    )
    return PartyLobbyService(deps)


def _lobby(*, guild_id=1, lobby_id="lobby-1", state=LobbyState.READY_CHECK):
    lobby = MagicMock()
    lobby.guild_id = guild_id
    lobby.lobby_id = lobby_id
    lobby.state = state
    return lobby


def _record(lobby):
    record = MagicMock()
    record.lobby = lobby
    return record


def test_feature_name_is_party_lobby():
    feature = PartyLobbyFeature(_service())
    assert feature.name == "party_lobby"


async def test_expire_skips_non_ready_check_lobbies():
    party_repository = MagicMock()
    party_repository.recover_active.return_value = [_record(_lobby(state=LobbyState.OPEN))]
    queue_service = MagicMock()
    queue_service.expire = AsyncMock()
    service = _service(party_repository=party_repository, party_queue_service=queue_service)

    ctx = LifecycleContext(get_guild=lambda gid: None)
    await service.expire_ready_checks(ctx)

    queue_service.expire.assert_not_called()


async def test_expire_skips_when_not_timed_out():
    party_repository = MagicMock()
    party_repository.recover_active.return_value = [_record(_lobby())]
    queue_service = MagicMock()
    queue_service.expire = AsyncMock(return_value=(MagicMock(), ()))
    service = _service(party_repository=party_repository, party_queue_service=queue_service)

    ctx = LifecycleContext(get_guild=lambda gid: None)
    await service.expire_ready_checks(ctx)

    party_repository.transition.assert_not_called()


async def test_expire_cancels_lobby_and_notifies_channel():
    party_repository = MagicMock()
    party_repository.recover_active.return_value = [_record(_lobby())]
    queue = MagicMock()
    queue.status = QueueStatus.CANCELLED
    queue.ready_deadline = "deadline"
    queue_service = MagicMock()
    queue_service.expire = AsyncMock(return_value=(queue, (10, 20)))
    settings_module = MagicMock()
    settings_module.get_guild_settings.return_value = {
        "managed": {"playChannelId": "555"}
    }
    service = _service(
        party_repository=party_repository,
        party_queue_service=queue_service,
        settings_module=settings_module,
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    ctx = LifecycleContext(get_guild=lambda gid: None, get_channel=lambda cid: channel)
    await service.expire_ready_checks(ctx)

    party_repository.transition.assert_called_once()
    assert party_repository.transition.call_args.args[2] is LobbyState.CANCELLED
    channel.send.assert_awaited_once()
    reply = channel.send.call_args.args[0]
    assert "<@10>" in reply and "<@20>" in reply


async def test_expire_does_not_cancel_when_queue_not_cancelled():
    party_repository = MagicMock()
    party_repository.recover_active.return_value = [_record(_lobby())]
    queue = MagicMock()
    queue.status = QueueStatus.OPEN
    queue_service = MagicMock()
    queue_service.expire = AsyncMock(return_value=(queue, (10,)))
    settings_module = MagicMock()
    settings_module.get_guild_settings.return_value = {"managed": {"playChannelId": ""}}
    service = _service(
        party_repository=party_repository,
        party_queue_service=queue_service,
        settings_module=settings_module,
    )

    ctx = LifecycleContext(get_guild=lambda gid: None)
    await service.expire_ready_checks(ctx)

    party_repository.transition.assert_not_called()


async def test_feature_on_cleanup_delegates_to_service():
    party_repository = MagicMock()
    party_repository.recover_active.return_value = []
    service = _service(party_repository=party_repository)
    feature = PartyLobbyFeature(service)

    ctx = LifecycleContext(get_guild=lambda gid: None)
    await feature.on_cleanup(ctx)

    party_repository.recover_active.assert_called_once()


async def test_startup_recovery_refreshes_saved_private_formation_controls():
    lobby = PartyLobby(
        "lobby-1",
        guild_id=1,
        organizer_id=10,
        capacity=4,
        state=LobbyState.FORMING,
        delivery=DiscordDelivery(match_channel_id=555, formation_message_id=777),
    )
    party_repository = MagicMock()
    party_repository.recover_active.return_value = [_record(lobby)]
    room_service = MagicMock()
    room_service.get = AsyncMock(return_value=SimpleNamespace(text_room_id=555))
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    guild = MagicMock(id=1)
    guild.get_channel.return_value = channel
    service = _service(
        party_repository=party_repository,
        match_room_service_for_guild=lambda candidate: room_service,
    )

    await service.recover_match_controls(
        LifecycleContext(get_guild=lambda guild_id: guild)
    )

    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["view"] is not None
