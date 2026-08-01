"""Characterization tests for /party room in bot.py.

Written before extracting this handler into a feature module (Issue #48,
Phase 5b). Pins down current routing behavior — which action maps to which
MatchRoomService call, and error handling — using a mocked service so the
adapter's orchestration is tested independently of MatchRoomService's own
(already covered) internals.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


@pytest.fixture()
def room_service(monkeypatch):
    service = MagicMock()
    service.lock = AsyncMock()
    service.unlock = AsyncMock()
    service.remove_player = AsyncMock()
    service.transfer_transactionally = AsyncMock()
    service.move_players = AsyncMock(return_value={})
    service.close = AsyncMock()
    service.get = AsyncMock()
    service.get_report = AsyncMock()
    for method in (
        service.lock, service.unlock, service.remove_player,
        service.transfer_transactionally, service.close, service.get,
    ):
        method.return_value = MagicMock(lobby_id="lobby-12345678")
    monkeypatch.setattr(bot, "_match_room_service_for_guild", lambda guild: service)
    monkeypatch.setattr(
        bot._party_room_deps, "match_room_service_for_guild", lambda guild: service
    )
    return service


def _interaction(*, guild_id=1, user_id=100):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild = MagicMock(id=guild_id) if guild_id is not None else None
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def test_room_action_is_a_discord_selection_not_free_form_text():
    action = next(parameter for parameter in bot.party_room.parameters if parameter.name == "action")

    assert [(choice.name, choice.value) for choice in action.choices] == [
        ("Lock", "lock"),
        ("Unlock", "unlock"),
        ("Remove Player", "remove"),
        ("Transfer Organizer", "transfer"),
        ("Move to Team", "move"),
        ("Close Rooms", "close"),
    ]


async def test_requires_guild():
    interaction = _interaction(guild_id=None)
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="lock")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Server-only" in reply


async def test_lock_routes_to_service(room_service):
    interaction = _interaction()
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="lock")
    room_service.lock.assert_awaited_once_with("lobby-1", actor_id=100)
    reply = interaction.response.send_message.call_args.args[0]
    assert "lock" in reply


async def test_unlock_routes_to_service(room_service):
    interaction = _interaction()
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="unlock")
    room_service.unlock.assert_awaited_once_with("lobby-1", actor_id=100)


async def test_remove_requires_member(room_service):
    interaction = _interaction()
    await bot.party_room.callback(
        interaction, lobby_id="lobby-1", action="remove", member=None
    )
    room_service.remove_player.assert_not_awaited()
    reply = interaction.response.send_message.call_args.args[0]
    assert "Use lock, unlock" in reply


async def test_remove_routes_to_service_with_member(room_service):
    interaction = _interaction()
    member = MagicMock(id=555)
    await bot.party_room.callback(
        interaction, lobby_id="lobby-1", action="remove", member=member
    )
    room_service.remove_player.assert_awaited_once_with(
        "lobby-1", actor_id=100, user_id=555
    )


async def test_close_routes_to_service(room_service):
    interaction = _interaction()
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="close")
    room_service.close.assert_awaited_once_with(
        "lobby-1",
        actor_id=100,
        reason="organizer closed rooms",
        outcome="cancelled",
    )


async def test_move_reports_failure_without_completing(room_service):
    room_service.move_players.return_value = {555: "That player is not queued."}
    interaction = _interaction()
    member = MagicMock(id=555)
    voice = MagicMock(id=777)
    await bot.party_room.callback(
        interaction, lobby_id="lobby-1", action="move",
        member=member, lobby_voice=voice, team=1,
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert reply == "That player is not queued."
    room_service.get.assert_not_awaited()


async def test_move_success_completes(room_service):
    interaction = _interaction()
    member = MagicMock(id=555)
    voice = MagicMock(id=777)
    await bot.party_room.callback(
        interaction, lobby_id="lobby-1", action="move",
        member=member, lobby_voice=voice, team=1,
    )
    room_service.move_players.assert_awaited_once_with(
        "lobby-1", actor_id=100, lobby_voice_id=777, team_assignments={555: 1}
    )
    room_service.get.assert_awaited_once_with("lobby-1")


async def test_unknown_action_reports_usage(room_service):
    interaction = _interaction()
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="bogus")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Use lock, unlock" in reply


async def test_service_error_is_reported(room_service):
    room_service.lock.side_effect = PermissionError("not the organizer")
    interaction = _interaction()
    await bot.party_room.callback(interaction, lobby_id="lobby-1", action="lock")
    reply = interaction.response.send_message.call_args.args[0]
    assert "not the organizer" in reply


async def test_report_returns_guild_scoped_human_readable_embed(room_service):
    report = MagicMock(
        report_id="report-123",
        lobby_id="lobby-12345678",
        organizer_id=100,
        participant_ids=(100, 200),
        outcome=MagicMock(value="cancelled"),
        close_reason="organizer closed rooms",
        closed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    room_service.get_report.return_value = report
    interaction = _interaction()
    interaction.user.guild_permissions.manage_guild = False

    await bot.party_report.callback(interaction, reference="lobby-12345678")

    room_service.get_report.assert_awaited_once_with(
        "lobby-12345678", guild_id=1, actor_id=100, is_staff=False
    )
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Match room report"
    assert "report-123" in kwargs["embed"].description
    assert "```json" not in kwargs["embed"].description


async def test_report_rejects_cross_guild_or_unauthorized_lookup(room_service):
    room_service.get_report.side_effect = PermissionError(
        "only the organizer or server staff can view this report"
    )
    interaction = _interaction(guild_id=2, user_id=200)
    interaction.user.guild_permissions.manage_guild = False

    await bot.party_report.callback(interaction, reference="report-123")

    room_service.get_report.assert_awaited_once_with(
        "report-123", guild_id=2, actor_id=200, is_staff=False
    )
    assert "organizer or server staff" in interaction.response.send_message.call_args.args[0]
