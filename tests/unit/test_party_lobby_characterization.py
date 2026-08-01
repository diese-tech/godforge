"""Characterization tests for the play-panel/lobby-card/queue/ready-check/
draft-launch handlers in bot.py.

Written before extracting these handlers into feature modules (Issue #48,
Phase 5d). This is the largest and most tightly-coupled remaining surface —
pins down current behavior for _handle_play_panel_action,
_handle_create_lobby_submission, _join_lobby_from_preferences,
_ensure_party_queue, _handle_lobby_card_action, _launch_party_draft, and
_handle_ready_check_action using real repositories (SQLitePartyRepository,
PartyQueueService) and mocked Discord interactions/channels/guilds.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.party import DiscordDelivery, LobbyState, Participant
from utils.party_draft import PartyDraftLaunchRepository
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService
from utils.party_store import SQLitePartyRepository
from utils.match_history import MatchHistoryRepository
from utils.scrims import ScrimRepository


@pytest.fixture()
def party_repos(tmp_path, monkeypatch):
    party = SQLitePartyRepository(tmp_path / "party.db")
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    party_draft = PartyDraftLaunchRepository(tmp_path / "party.db")
    match_history = MatchHistoryRepository(tmp_path / "party.db")
    scrims = ScrimRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "party_repository", party)
    monkeypatch.setattr(bot, "party_queue_service", queue_service)
    monkeypatch.setattr(bot, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot, "match_history_repository", match_history)
    monkeypatch.setattr(bot, "scrim_repository", scrims)
    monkeypatch.setattr(bot._match_action_deps, "match_history_repository", match_history)
    monkeypatch.setattr(bot._match_action_deps, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot._match_action_deps, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._party_lobby_deps, "party_repository", party)
    monkeypatch.setattr(bot._party_lobby_deps, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._party_lobby_deps, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot._party_lobby_deps, "scrim_repository", scrims)
    return party, queue_service, party_draft, match_history, scrims


def _guild(guild_id=1, *, members=None):
    guild = MagicMock()
    guild.id = guild_id
    members = members or {}
    guild.get_member = lambda uid: members.get(uid)
    guild.get_channel = lambda cid: None
    return guild


def _interaction(*, guild_id=1, user_id=100, guild=None, channel=None, message=None):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    interaction.guild = guild if guild is not None else (_guild(guild_id) if guild_id else None)
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    channel = channel if channel is not None else MagicMock(id=54321)
    channel.send = AsyncMock(return_value=MagicMock(id=12345))
    interaction.channel = channel
    interaction.message = message
    return interaction


def _lobby_footer_message(lobby_id):
    footer = MagicMock(text=f"lobby_id={lobby_id}")
    embed = MagicMock(footer=footer)
    message = MagicMock(embeds=[embed])
    message.edit = AsyncMock()
    return message


# -- Play panel ---------------------------------------------------------

async def test_play_panel_preferences_shows_current_roles(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "preferences")
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "view" in kwargs


async def test_play_panel_browse_empty_reports_none(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "browse")
    reply = interaction.response.send_message.call_args.args[0]
    assert "No party lobbies" in reply


async def test_play_panel_create_opens_selection_wizard(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "create")
    interaction.response.send_modal.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["view"].children[0].custom_id == "godforge:lobby:create:mode:v2"


async def test_play_panel_browse_shows_open_lobby(party_repos):
    party, *_ = party_repos
    party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "browse")
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "embed" in kwargs


# -- Create lobby + join -------------------------------------------------

async def test_create_lobby_submission_creates_and_seats_organizer(party_repos, tmp_settings):
    party, queue_service, *_ = party_repos
    interaction = _interaction(user_id=100)
    payload = {
        "party_size": 10, "mode": "conquest", "region": "na", "format": "5v5",
        "voice_required": False, "skill_band": "", "notes": "",
    }
    await bot._handle_create_lobby_submission(interaction, payload)
    interaction.response.send_message.assert_awaited_once()
    lobbies = [r.lobby for r in party.recover_active(1)]
    assert len(lobbies) == 1
    assert lobbies[0].participants[0].user_id == 100
    queue = await queue_service.get(lobbies[0].lobby_id)
    assert queue is not None


async def test_create_lobby_submission_rejects_capacity_outside_supported_choices(
    party_repos, tmp_settings
):
    party, *_ = party_repos
    interaction = _interaction(user_id=100)
    payload = {
        "party_size": 3,
        "mode": "conquest",
        "region": "na east",
        "format": "pug",
        "voice_required": False,
        "skill_band": "open",
        "notes": "",
    }

    await bot._handle_create_lobby_submission(interaction, payload)

    assert party.recover_active(1) == []
    reply = interaction.response.send_message.await_args.args[0]
    assert "2, 4, 6, 8, or 10" in reply


async def test_join_lobby_from_preferences_adds_participant(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(100), operation_id="join-organizer")
    interaction = _interaction(user_id=200, message=_lobby_footer_message(lobby.lobby_id))
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False, "captain": False}
    await bot._join_lobby_from_preferences(interaction, lobby.lobby_id, payload)
    updated = party.get(1, lobby.lobby_id)
    assert any(p.user_id == 200 for p in updated.participants)


async def test_join_full_lobby_starts_ready_check(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False, "captain": False}
    await bot._join_lobby_from_preferences(interaction, lobby.lobby_id, payload)
    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.READY_CHECK
    interaction.channel.send.assert_awaited_once()


# -- Lobby card -----------------------------------------------------------

async def test_lobby_card_leave_removes_participant(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(100), operation_id="join-organizer")
    party.save_participant(1, lobby.lobby_id, Participant(200), operation_id="join-other")
    await bot._ensure_party_queue(party.get(1, lobby.lobby_id))
    interaction = _interaction(user_id=200, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "leave")
    updated = party.get(1, lobby.lobby_id)
    assert not any(p.user_id == 200 for p in updated.participants)


async def test_lobby_card_inactive_lobby_reports_error(party_repos):
    interaction = _interaction(message=_lobby_footer_message("missing-lobby"))
    await bot._handle_lobby_card_action(interaction, "leave")
    reply = interaction.response.send_message.call_args.args[0]
    assert "no longer active" in reply


async def test_lobby_card_cancel_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "cancel")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_lobby_card_cancel_by_organizer_transitions(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=100, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "cancel")
    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.CANCELLED


async def test_lobby_card_share_persists_public_projection_reference(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=4, operation_id="create-1")
    channel = MagicMock(id=600)
    channel.send = AsyncMock(return_value=MagicMock(id=601))
    interaction = _interaction(
        user_id=100,
        channel=channel,
        message=_lobby_footer_message(lobby.lobby_id),
    )

    await bot._handle_lobby_card_action(interaction, "share")

    delivery = party.get(1, lobby.lobby_id).delivery
    assert delivery.panel_channel_id == 600
    assert delivery.panel_message_id == 12345


async def test_lobby_card_ready_check_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "ready_check")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


# -- Ready check ------------------------------------------------------------

async def test_ready_check_all_ready_transitions_to_forming(party_repos, monkeypatch):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="join-2")
    party.set_delivery(
        1,
        lobby.lobby_id,
        DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="public-card",
    )
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    room_service = MagicMock()
    room_service.provision = AsyncMock(
        return_value=MagicMock(text_room_id=555, team_voice_ids=())
    )
    monkeypatch.setattr(bot, "_match_room_service_for_guild", lambda guild: room_service)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )

    private_channel = MagicMock(spec=bot.discord.TextChannel)
    private_channel.send = AsyncMock(return_value=MagicMock(id=777))
    public_message = MagicMock()
    public_message.edit = AsyncMock()
    public_channel = MagicMock()
    public_channel.fetch_message = AsyncMock(return_value=public_message)
    guild = _guild(1)
    guild.get_channel = lambda channel_id: (
        private_channel if channel_id == 555 else public_channel if channel_id == 600 else None
    )
    interaction1 = _interaction(
        user_id=1, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )
    await bot._handle_ready_check_action(interaction1, "ready")

    interaction2 = _interaction(
        user_id=2, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )
    await bot._handle_ready_check_action(interaction2, "ready")

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.FORMING
    assert updated.delivery.match_channel_id == 555
    assert updated.delivery.formation_message_id == 777
    reply = interaction2.response.edit_message.call_args.kwargs["content"]
    assert "forming the match" in reply
    private_channel.send.assert_awaited_once()
    private_card = private_channel.send.await_args.kwargs
    assert private_card["view"].timeout is None
    assert "formation" in private_card["embed"].title.lower()
    public_message.edit.assert_awaited_once()
    public_embed = public_message.edit.await_args.kwargs["embed"]
    assert any(
        field.name == "Status" and "Match forming" in field.value
        for field in public_embed.fields
    )


async def test_ready_check_drop_reopens_lobby(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="join-2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_ready_check_action(interaction, "drop")

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.OPEN
    assert not any(p.user_id == 2 for p in updated.participants)


async def test_ready_check_rejects_odd_roster_before_room_formation(
    party_repos, monkeypatch
):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create")
    for user_id in range(1, 4):
        party.save_participant(
            1, lobby.lobby_id, Participant(user_id), operation_id=f"join-{user_id}"
        )
    await queue_service.create(lobby.lobby_id, 4)
    for user_id in range(1, 4):
        await queue_service.join(lobby.lobby_id, user_id, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")
    room_service = MagicMock()
    room_service.provision = AsyncMock()
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    final_interaction = None
    for user_id in range(1, 4):
        final_interaction = _interaction(
            user_id=user_id, message=_lobby_footer_message(lobby.lobby_id)
        )
        await bot._handle_ready_check_action(final_interaction, "ready")

    assert party.get(1, lobby.lobby_id).state is LobbyState.READY_CHECK
    room_service.provision.assert_not_awaited()
    content = final_interaction.response.edit_message.await_args.kwargs["content"].lower()
    assert "wait for one player" in content
    assert "drop one player" in content
    assert "substitute" in content


# -- Draft launch -------------------------------------------------------

async def test_launch_party_draft_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999)
    await bot._launch_party_draft(interaction, lobby)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_launch_party_draft_requires_forming_state(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=100)
    await bot._launch_party_draft(interaction, lobby)
    reply = interaction.response.send_message.call_args.args[0]
    assert "ready check" in reply.lower()


async def test_launch_party_draft_reports_missing_private_room(party_repos, monkeypatch):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create")
    for user_id in (1, 2):
        lobby = party.save_participant(
            1, lobby.lobby_id, Participant(user_id), operation_id=f"join-{user_id}"
        )
    for state, operation in (
        (LobbyState.FULL, "full"),
        (LobbyState.READY_CHECK, "ready"),
        (LobbyState.FORMING, "forming"),
    ):
        lobby = party.transition(1, lobby.lobby_id, state, operation_id=operation)
    room_service = MagicMock()
    room_service.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    interaction = _interaction(user_id=1)

    await bot._launch_party_draft(interaction, lobby)

    reply = interaction.response.send_message.await_args.args[0].lower()
    assert "private match room is missing" in reply
    assert "retry formation" in reply


async def test_launch_party_draft_local_success(party_repos, monkeypatch):
    from utils.draft import DraftManager

    party, queue_service, party_draft, match_history, scrims = party_repos
    ROLES = ("solo", "jungle", "mid", "support", "adc")
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    members = {}
    for user_id, role in enumerate(ROLES * 2, start=1):
        lobby = party.save_participant(
            1, lobby.lobby_id,
            Participant(user_id, primary_role=role, captain=user_id in {1, 6}),
            operation_id=f"j{user_id}",
        )
        members[user_id] = MagicMock(id=user_id, display_name=f"Player{user_id}")
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="rc")
    lobby = party.transition(1, lobby.lobby_id, LobbyState.FORMING, operation_id="forming")

    drafts = DraftManager()
    monkeypatch.setattr(bot, "drafts", drafts)
    monkeypatch.setattr(bot._party_lobby_deps, "drafts", drafts)
    monkeypatch.setattr(bot.activity_client, "base_url", "")

    private_channel = MagicMock(id=700, name="match-private")
    private_channel.send = AsyncMock(return_value=MagicMock(id=701))
    room_service = MagicMock()
    room_service.get = AsyncMock(return_value=MagicMock(text_room_id=700))
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    guild = _guild(1, members=members)
    guild.get_channel = lambda channel_id: private_channel if channel_id == 700 else None
    interaction = _interaction(user_id=1, guild=guild)
    interaction.channel.name = "party-draft"

    await bot._launch_party_draft(interaction, lobby)

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.ACTIVE
    launch = party_draft.get(lobby.lobby_id)
    assert launch.status == "active"
    assert drafts.get(private_channel.id) is not None
    assert drafts.get(interaction.channel.id) is None
    assert party_draft.get(lobby.lobby_id).channel_id == private_channel.id
    # A directly recovered FORMING lobby first recreates its formation card,
    # then posts the launch summary and result card in the same private channel.
    assert private_channel.send.await_count == 3
    interaction.followup.send.assert_awaited()


async def test_private_formation_control_moves_saved_teams_with_partial_failures(
    party_repos, monkeypatch
):
    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create",
        voice_required=True,
    )
    for user_id in range(1, 5):
        lobby = party.save_participant(
            1, lobby.lobby_id, Participant(user_id), operation_id=f"join-{user_id}"
        )
    for state, operation in (
        (LobbyState.FULL, "full"),
        (LobbyState.READY_CHECK, "ready"),
        (LobbyState.FORMING, "forming"),
        (LobbyState.ACTIVE, "active"),
    ):
        lobby = party.transition(1, lobby.lobby_id, state, operation_id=operation)
    launch_repo = MagicMock()
    launch_repo.get.return_value = SimpleNamespace(
        status="active",
        blue=SimpleNamespace(participant_ids=(1, 2)),
        red=SimpleNamespace(participant_ids=(3, 4)),
    )
    room_service = MagicMock()
    room_service.move_players = AsyncMock(return_value={4: "Player is not connected to voice."})
    monkeypatch.setattr(bot._party_lobby_deps, "party_draft_repository", launch_repo)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    interaction = _interaction(
        user_id=1, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot._handle_lobby_card_action(interaction, "move_teams_voice")

    room_service.move_players.assert_awaited_once_with(
        lobby.lobby_id,
        actor_id=1,
        lobby_voice_id=None,
        team_assignments={1: 1, 2: 1, 3: 2, 4: 2},
    )
    reply = interaction.response.send_message.await_args.args[0]
    assert "3 moved" in reply
    assert "<@4>" in reply
    assert "not connected" in reply
