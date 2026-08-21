"""Regression coverage for the Issue #63 queue-first UX hardening pass.

These tests exercise the same real repositories/services as
``test_party_lobby_characterization.py`` (SQLitePartyRepository,
PartyQueueService) with mocked Discord interactions/channels/guilds, and are
scoped specifically to the behaviors #63 introduced or changed:

- the ephemeral-join/public-card desynchronization bug and its fix
- returning-player saved-preference fast join + first-time role wizard
- waitlist join/promotion
- auto-publish on queue creation (no Share step required)
- queue rename preserving stable identity/code
- organizer transfer (manual and automatic-on-leave)
- one-active-queue-per-player-per-guild enforcement
- recruiting inactivity expiry
- multi-queue isolation and explicit-choice routing
- idempotent final-Ready handoff (no duplicate rooms/pings)
- restart recovery of controls for every active queue
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.party import DiscordDelivery, LobbyState, Participant
from utils.party_draft import PartyDraftLaunchRepository
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService, QueueStatus
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


def _guild(guild_id=1, *, members=None, channels=None):
    guild = MagicMock()
    guild.id = guild_id
    members = members or {}
    channels = channels or {}
    guild.get_member = lambda uid: members.get(uid)
    guild.get_channel = lambda cid: channels.get(cid)
    return guild


def _channel(channel_id=999, *, with_fetch=None):
    channel = MagicMock(id=channel_id)
    channel.send = AsyncMock(return_value=MagicMock(id=channel_id * 10))
    if with_fetch is not None:
        channel.fetch_message = AsyncMock(return_value=with_fetch)
    return channel


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


def _ephemeral_wizard_message():
    """A message with no lobby_id footer at all — models an ephemeral wizard."""
    message = MagicMock(embeds=[])
    message.edit = AsyncMock()
    return message


# -- 1. The core desync bug: ephemeral interaction message must never be the
#    canonical public card, and the real public card must always refresh. --


async def test_join_via_ephemeral_wizard_never_edits_the_wizard_message(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    public_message = MagicMock()
    public_message.edit = AsyncMock()
    public_channel = MagicMock()
    public_channel.fetch_message = AsyncMock(return_value=public_message)
    guild = _guild(1, channels={600: public_channel})
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    # The join click's own "message" is the ephemeral wizard, not the public
    # card — it has no lobby_id footer and must never be written to.
    wizard_message = _ephemeral_wizard_message()
    interaction = _interaction(user_id=2, guild=guild, message=wizard_message)
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False}

    await bot._join_lobby_from_preferences(interaction, lobby.lobby_id, payload)

    # Durable state updated.
    updated = party.get(1, lobby.lobby_id)
    assert any(p.user_id == 2 for p in updated.participants)
    queue = await queue_service.get(lobby.lobby_id)
    assert any(member.user_id == 2 for member in queue.active)
    # The wizard's own message was never touched.
    wizard_message.edit.assert_not_awaited()
    # The real, durably-referenced public card was refreshed instead.
    public_message.edit.assert_awaited_once()
    refreshed_embed = public_message.edit.await_args.kwargs["embed"]
    assert "2/4" in str(refreshed_embed.to_dict())
    # Player receives an explicit success acknowledgement.
    ack = interaction.response.send_message.await_args.kwargs.get(
        "content"
    ) or interaction.response.send_message.await_args.args[0]
    assert "Joined" in ack


async def test_join_via_lobby_card_button_still_refreshes_via_delivery_reference(party_repos):
    """Even a click on the real public card must go through the same
    delivery-reference refresh path, not a local interaction.message.edit."""
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    public_message = MagicMock()
    public_message.edit = AsyncMock()
    public_channel = MagicMock()
    public_channel.fetch_message = AsyncMock(return_value=public_message)
    guild = _guild(1, channels={600: public_channel})
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    card_message = _lobby_footer_message(lobby.lobby_id)
    interaction = _interaction(user_id=2, guild=guild, message=card_message)

    await bot._handle_lobby_card_action(interaction, "join")

    # First-time player (no saved prefs) gets the role wizard, not an
    # immediate join — and the click never touches the card's own message.
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "view" in kwargs
    assert kwargs["view"].children[0].custom_id == "godforge:lobby:join:primary:v2"
    card_message.edit.assert_not_awaited()
    public_message.edit.assert_not_awaited()  # nothing changed yet — no join happened


# -- 2. Returning player fast path vs first-time wizard ----------------------


async def test_returning_player_joins_instantly_with_saved_preferences(party_repos):
    party, queue_service, *_ = party_repos
    from utils.party import PlayerPreferences

    party.set_player_preferences(
        1, 7, PlayerPreferences("support", "solo", True, False)
    )
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    interaction = _interaction(user_id=7, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "join")

    updated = party.get(1, lobby.lobby_id)
    participant = updated.participant(7)
    assert participant is not None
    assert participant.primary_role == "support"
    assert participant.secondary_role == "solo"
    # No role wizard was shown — only the ack (with a Change Roles view).
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "view" in kwargs and kwargs["view"] is not None
    ack = interaction.response.send_message.await_args.args[0]
    assert "Joined" in ack and "support/solo" in ack


async def test_first_time_player_only_sees_primary_secondary_fill(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    interaction = _interaction(user_id=8, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "join")

    kwargs = interaction.response.send_message.await_args.kwargs
    view = kwargs["view"]
    custom_ids = [item.custom_id for item in view.children]
    assert custom_ids == [
        "godforge:lobby:join:primary:v2",
        "godforge:lobby:join:secondary:v2",
        "godforge:lobby:join:fill:v2",
        "godforge:lobby:join:confirm:v2",
        "godforge:lobby:join:cancel:v2",
    ]


# -- 3. Waitlist join/promotion ----------------------------------------------


async def test_waitlist_join_reports_position_then_promotes_on_leave(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="join-2")
    await bot._ensure_party_queue(party.get(1, lobby.lobby_id))

    waitlisted = _interaction(user_id=3, message=_lobby_footer_message(lobby.lobby_id))
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False}
    await bot._join_lobby_from_preferences(waitlisted, lobby.lobby_id, payload)
    ack = waitlisted.response.send_message.await_args.args[0]
    assert "waitlist" in ack.lower() and "position 1" in ack

    leave_interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(leave_interaction, "leave")

    queue = await queue_service.get(lobby.lobby_id)
    assert {member.user_id for member in queue.active} == {2, 3}
    assert queue.waitlist == []


# -- 4. Auto-publish on creation; no Share step required ---------------------


async def test_queue_creation_auto_publishes_without_a_share_step(party_repos, tmp_settings):
    import utils.settings as settings_mod

    party, *_ = party_repos
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "600"}})
    play_channel = MagicMock(id=600)
    play_channel.send = AsyncMock(return_value=MagicMock(id=601))
    guild = _guild(1, channels={600: play_channel})
    interaction = _interaction(user_id=100, guild=guild)
    payload = {
        "party_size": 10, "mode": "conquest", "region": "na", "format": "5v5",
        "voice_required": False, "skill_band": "", "notes": "", "queue_name": "Inhouses",
    }

    await bot._handle_create_lobby_submission(interaction, payload)

    play_channel.send.assert_awaited_once()
    posted_view = play_channel.send.await_args.kwargs["view"]
    action_ids = {item.action for item in posted_view.children}
    assert action_ids == {"join", "leave", "queue_settings", "cancel"}
    lobby = party.recover_active(1)[0].lobby
    assert lobby.delivery.panel_channel_id == 600
    assert lobby.delivery.panel_message_id == 601
    assert lobby.display_name == "Inhouses"
    confirmation = interaction.response.send_message.await_args.kwargs["content"]
    assert "live in the Play channel" in confirmation


# -- 5. Rename preserves stable identity/code --------------------------------


async def test_rename_preserves_lobby_id_and_queue_code(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    original_code = lobby.queue_code
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "rename")

    modal = interaction.response.send_modal.await_args.args[0]
    # Simulates the organizer having typed a mention into the modal's text
    # input — exercised via the modal's ``default`` (discord.py populates
    # ``.value`` from real user input the same way at submit time).
    modal = type(modal)(modal._on_submit, "Weekend <@999> Warriors  ")
    await modal.on_submit(interaction)

    renamed = party.get(1, lobby.lobby_id)
    assert renamed.lobby_id == lobby.lobby_id
    assert renamed.queue_code == original_code
    assert renamed.display_name == "Weekend Warriors"
    assert "<@" not in renamed.display_name


async def test_non_organizer_cannot_rename(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    interaction = _interaction(user_id=999, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "rename")

    interaction.response.send_modal.assert_not_awaited()
    reply = interaction.response.send_message.await_args.args[0]
    assert "Only the organizer" in reply


# -- 6. Organizer transfer: manual and automatic-on-leave --------------------


async def test_manual_organizer_transfer_via_queue_settings(party_repos, monkeypatch):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    captured = {}

    def capture_view(handler, participants):
        captured["handler"] = handler
        captured["participants"] = participants
        from utils.lobby_views import TransferOrganizerView
        return TransferOrganizerView(handler, participants)

    monkeypatch.setattr(bot._party_lobby_deps, "transfer_organizer_view", capture_view)

    await bot._handle_lobby_card_action(interaction, "transfer_organizer")

    assert [user_id for user_id, _label in captured["participants"]] == [2]
    follow_up = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))
    await captured["handler"](follow_up, 2)

    assert party.get(1, lobby.lobby_id).organizer_id == 2


async def test_organizer_leaving_transfers_to_longest_tenured_participant(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    party.save_participant(1, lobby.lobby_id, Participant(3), operation_id="j3")
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "leave")

    updated = party.get(1, lobby.lobby_id)
    assert updated.organizer_id == 2  # earliest remaining joiner
    assert not any(p.user_id == 1 for p in updated.participants)


async def test_organizer_leaving_empty_queue_cancels_it(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "leave")

    assert party.get(1, lobby.lobby_id).state is LobbyState.CANCELLED


# -- 7. One active queue per player per guild --------------------------------


async def test_cannot_join_a_second_queue_while_active_in_another(party_repos):
    party, *_ = party_repos
    first = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, first.lobby_id, Participant(9), operation_id="j9")
    second = party.create(guild_id=1, organizer_id=2, capacity=4, operation_id="create-2")
    party.save_participant(1, second.lobby_id, Participant(2), operation_id="j2")
    interaction = _interaction(user_id=9, message=_lobby_footer_message(second.lobby_id))

    await bot._handle_lobby_card_action(interaction, "join")

    updated = party.get(1, second.lobby_id)
    assert not any(p.user_id == 9 for p in updated.participants)
    reply = interaction.response.send_message.await_args.args[0]
    assert "already in" in reply
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert view is not None  # Leave/recovery action offered


async def test_leaving_the_other_queue_view_action_frees_the_player_up(
    party_repos, monkeypatch
):
    party, *_ = party_repos
    from utils.lobby_views import AlreadyInQueueView

    first = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, first.lobby_id, Participant(9), operation_id="j9")
    second = party.create(guild_id=1, organizer_id=2, capacity=4, operation_id="create-2")
    party.save_participant(1, second.lobby_id, Participant(2), operation_id="j2")
    interaction = _interaction(user_id=9, message=_lobby_footer_message(second.lobby_id))

    captured = {}

    def capture_view(handler):
        captured["handler"] = handler
        return AlreadyInQueueView(handler)

    monkeypatch.setattr(bot._party_lobby_deps, "already_in_queue_view", capture_view)
    await bot._handle_lobby_card_action(interaction, "join")

    leave_click = _interaction(user_id=9, guild=interaction.guild)
    await captured["handler"](leave_click)

    assert not any(p.user_id == 9 for p in party.get(1, first.lobby_id).participants)


# -- 8. Recruiting inactivity expiry -----------------------------------------


async def test_recruiting_queue_awaits_inactivity_prompt_instead_of_hard_expiring(
    party_repos,
):
    # Issue #66: an elapsed recruiting clock no longer expires the queue
    # directly — see the two-stage flow tested in section 8a/8b below.
    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    active = party.recover_active(1)

    assert len(active) == 1
    assert active[0].lobby.lobby_id == lobby.lobby_id
    assert party.get(1, lobby.lobby_id).state is LobbyState.OPEN


async def test_join_activity_extends_the_expiry_clock(party_repos):
    party, *_ = party_repos
    soon = datetime.now(timezone.utc) + timedelta(seconds=5)
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1", expires_at=soon,
    )
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")

    touched = party.touch_recruiting_activity(1, lobby.lobby_id, operation_id="touch-1")

    assert touched.expires_at > soon


# -- 9. Multi-queue isolation and explicit-choice routing --------------------


async def test_find_a_queue_forces_explicit_choice_when_multiple_are_open(party_repos):
    party, *_ = party_repos
    party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.create(guild_id=1, organizer_id=2, capacity=4, operation_id="create-2")
    interaction = _interaction(user_id=50)

    await bot._handle_play_panel_action(interaction, "queue")

    kwargs = interaction.response.send_message.await_args.kwargs
    view = kwargs["view"]
    select = view.children[0]
    assert len(select.options) == 2
    reply = interaction.response.send_message.await_args.args[0]
    assert "Multiple queues" in reply


async def test_find_a_queue_shortcuts_when_exactly_one_is_open(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    interaction = _interaction(user_id=50)

    await bot._handle_play_panel_action(interaction, "queue")

    # Shortcut straight into the (first-time) join wizard, no queue picker.
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "view" in kwargs
    custom_ids = [item.custom_id for item in kwargs["view"].children]
    assert custom_ids[0] == "godforge:lobby:join:primary:v2"


async def test_second_queue_filling_does_not_touch_the_first_queues_card(party_repos):
    party, queue_service, *_ = party_repos
    first = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, first.lobby_id, Participant(1), operation_id="j1")
    first_channel = MagicMock(id=600)
    first_message = MagicMock()
    first_message.edit = AsyncMock()
    first_channel.fetch_message = AsyncMock(return_value=first_message)
    party.set_delivery(
        1, first.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="d1",
    )

    second = party.create(guild_id=1, organizer_id=2, capacity=2, operation_id="create-2")
    party.save_participant(1, second.lobby_id, Participant(2), operation_id="j2")
    second_channel = MagicMock(id=700)
    second_channel.send = AsyncMock(return_value=MagicMock(id=999))
    second_message = MagicMock()
    second_message.edit = AsyncMock()
    second_channel.fetch_message = AsyncMock(return_value=second_message)
    party.set_delivery(
        1, second.lobby_id, DiscordDelivery(panel_channel_id=700, panel_message_id=701),
        operation_id="d2",
    )

    guild = _guild(1, channels={600: first_channel, 700: second_channel})
    interaction = _interaction(
        user_id=3, guild=guild, message=_lobby_footer_message(second.lobby_id)
    )
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False}

    await bot._join_lobby_from_preferences(interaction, second.lobby_id, payload)

    # The join, and then the automatic full->ready-check transition it
    # triggers, both legitimately refresh *this* queue's card.
    assert second_message.edit.await_count >= 1
    first_message.edit.assert_not_awaited()
    assert not any(p.user_id == 3 for p in party.get(1, first.lobby_id).participants)


# -- 10. Idempotent final-Ready handoff ---------------------------------------


async def test_duplicate_final_ready_does_not_duplicate_room_or_ping(party_repos, monkeypatch):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="d1",
    )
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")
    await queue_service.respond(lobby.lobby_id, 1, "ready")

    room_service = MagicMock()
    room_service.provision = AsyncMock(
        return_value=MagicMock(text_room_id=555, team_voice_ids=())
    )
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    private_channel = MagicMock(spec=bot.discord.TextChannel)
    private_channel.send = AsyncMock(return_value=MagicMock(id=777))
    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    public_channel.send = AsyncMock(return_value=MagicMock(id=888))
    guild = _guild(1, channels={555: private_channel, 600: public_channel})

    # Two "final Ready" interactions for the same player/lobby racing/retried.
    interaction_a = _interaction(user_id=2, guild=guild, message=_lobby_footer_message(lobby.lobby_id))
    interaction_b = _interaction(user_id=2, guild=guild, message=_lobby_footer_message(lobby.lobby_id))
    import asyncio
    await asyncio.gather(
        bot._handle_ready_check_action(interaction_a, "ready"),
        bot._handle_ready_check_action(interaction_b, "ready"),
    )

    assert room_service.provision.await_count == 1
    assert public_channel.send.await_count == 1
    assert party.get(1, lobby.lobby_id).delivery.match_ready_notified is True
    assert party.get(1, lobby.lobby_id).state is LobbyState.FORMING


# -- 11. Restart recovery for every active queue -----------------------------


async def test_restart_recovery_restores_controls_for_every_active_stage(
    party_repos, monkeypatch, tmp_settings
):
    import utils.settings as settings_mod
    from utils.lifecycle import LifecycleContext

    party, queue_service, *_ = party_repos

    # An OPEN queue with no delivery yet — recovery should publish it.
    open_lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-open")
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "600"}})

    # A READY_CHECK queue whose ready-check card needs to still be reachable.
    rc_lobby = party.create(guild_id=1, organizer_id=2, capacity=2, operation_id="create-rc")
    party.save_participant(1, rc_lobby.lobby_id, Participant(1), operation_id="rc-j1")
    party.save_participant(1, rc_lobby.lobby_id, Participant(2), operation_id="rc-j2")
    await queue_service.create(rc_lobby.lobby_id, 2)
    await queue_service.join(rc_lobby.lobby_id, 1, ())
    await queue_service.join(rc_lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(rc_lobby.lobby_id)
    party.transition(1, rc_lobby.lobby_id, LobbyState.FULL, operation_id="rc-full")
    party.transition(1, rc_lobby.lobby_id, LobbyState.READY_CHECK, operation_id="rc-ready")
    # In real operation this queue's public card was already auto-published
    # at creation time, so recovery has a channel to post the ready-check
    # card into.
    party.set_delivery(
        1, rc_lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=602),
        operation_id="rc-delivery",
    )

    play_channel = MagicMock(id=600)
    play_channel.send = AsyncMock(return_value=MagicMock(id=601))
    play_channel.fetch_message = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    guild = _guild(1, channels={600: play_channel})
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None

    await bot.party_lobby_service.recover_match_controls(ctx)

    # open_lobby: publishes a fresh public card (no delivery yet).
    # rc_lobby: refreshes its existing public card + posts its ready-check card.
    assert play_channel.send.await_count == 2
    assert party.get(1, open_lobby.lobby_id).delivery.panel_channel_id == 600
    assert party.get(1, rc_lobby.lobby_id).delivery.ready_channel_id == 600


# -- Post-review-comment fixes -----------------------------------------------


async def test_manual_ready_check_rejects_odd_or_undersized_roster(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    party.save_participant(1, lobby.lobby_id, Participant(3), operation_id="j3")
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "ready_check")

    assert party.get(1, lobby.lobby_id).state is LobbyState.OPEN
    reply = interaction.response.send_message.await_args.args[0]
    assert "even roster" in reply.lower()


# -- 8a. Inactivity confirmation prompt (Issue #66) --------------------------


async def test_periodic_sweep_posts_inactivity_prompt_for_a_quiet_queue(
    party_repos, tmp_settings,
):
    import utils.settings as settings_mod
    from utils.lifecycle import LifecycleContext

    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    play_channel = MagicMock(id=700)
    play_channel.send = AsyncMock(return_value=MagicMock(id=800))
    guild = _guild(1, channels={700: play_channel})
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None
    ctx.get_channel = lambda channel_id: None

    await bot.party_lobby_service.expire_ready_checks(ctx)

    play_channel.send.assert_awaited_once()
    kwargs = play_channel.send.await_args.kwargs
    assert kwargs["content"] == f"<@{lobby.organizer_id}>"
    assert "still recruiting" in kwargs["embed"].title.lower()
    changed = party.get(1, lobby.lobby_id)
    assert changed.state is LobbyState.OPEN
    assert changed.delivery.inactivity_prompt_channel_id == 700
    assert changed.delivery.inactivity_prompt_message_id == 800
    assert changed.expires_at > lobby.expires_at


async def test_periodic_sweep_auto_closes_and_deletes_card_after_unanswered_grace(
    party_repos,
):
    from utils.lifecycle import LifecycleContext

    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 600, 602, operation_id="prompt-1", grace_minutes=-1,
    )
    public_card_message = MagicMock()
    public_card_message.delete = AsyncMock()
    prompt_message = MagicMock()
    prompt_message.delete = AsyncMock()

    def fetch_message(message_id):
        return {601: public_card_message, 602: prompt_message}[message_id]

    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(side_effect=fetch_message)
    guild = _guild(1, channels={600: public_channel})
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None
    ctx.get_channel = lambda channel_id: None

    await bot.party_lobby_service.expire_ready_checks(ctx)

    assert party.get(1, lobby.lobby_id).state is LobbyState.EXPIRED
    public_card_message.delete.assert_awaited_once()
    prompt_message.delete.assert_awaited_once()
    changed_delivery = party.get(1, lobby.lobby_id).delivery
    assert changed_delivery.panel_channel_id is None
    assert changed_delivery.panel_message_id is None


async def test_leaving_via_recovery_during_ready_check_reopens_the_queue(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")
    await queue_service.respond(lobby.lobby_id, 1, "ready")

    interaction = _interaction(user_id=2)
    changed = await bot.party_lobby_service._process_leave(interaction, lobby.lobby_id, 2)

    assert changed.state is LobbyState.OPEN
    queue = await queue_service.get(lobby.lobby_id)
    assert queue.status is QueueStatus.OPEN
    assert queue.ready == {}
    assert not any(p.user_id == 2 for p in changed.participants)


async def test_create_queue_rejects_organizer_already_active_elsewhere(party_repos):
    party, *_ = party_repos
    first = party.create(guild_id=1, organizer_id=9, capacity=4, operation_id="create-1")
    party.save_participant(1, first.lobby_id, Participant(9), operation_id="j9")
    interaction = _interaction(user_id=9)
    payload = {
        "party_size": 4, "mode": "conquest", "region": "na", "format": "5v5",
        "voice_required": False, "skill_band": "", "notes": "",
    }

    await bot._handle_create_lobby_submission(interaction, payload)

    # Only the original queue exists — no second queue was created.
    assert len(party.recover_active(1)) == 1
    reply = interaction.response.send_message.await_args.args[0]
    assert "already in" in reply


async def test_waitlisted_player_is_caught_by_the_one_queue_guard(party_repos):
    party, queue_service, *_ = party_repos
    first = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, first.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, first.lobby_id, Participant(3), operation_id="j3")
    await bot._ensure_party_queue(party.get(1, first.lobby_id))
    # User 5 waitlists on the first (full) queue — never written to
    # party_participants, only tracked in the live queue's waitlist lane.
    waitlist_interaction = _interaction(
        user_id=5, message=_lobby_footer_message(first.lobby_id)
    )
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False}
    await bot._join_lobby_from_preferences(waitlist_interaction, first.lobby_id, payload)
    queue = await queue_service.get(first.lobby_id)
    assert any(member.user_id == 5 for member in queue.waitlist)

    second = party.create(guild_id=1, organizer_id=2, capacity=4, operation_id="create-2")
    party.save_participant(1, second.lobby_id, Participant(2), operation_id="j2")
    second_interaction = _interaction(
        user_id=5, message=_lobby_footer_message(second.lobby_id)
    )

    await bot._handle_lobby_card_action(second_interaction, "join")

    assert not any(p.user_id == 5 for p in party.get(1, second.lobby_id).participants)
    reply = second_interaction.response.send_message.await_args.args[0]
    assert "already in" in reply


async def test_rename_extends_the_recruiting_expiry_clock(party_repos):
    party, *_ = party_repos
    soon = datetime.now(timezone.utc) + timedelta(seconds=5)
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1", expires_at=soon,
    )
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "rename")
    modal = interaction.response.send_modal.await_args.args[0]
    modal = type(modal)(modal._on_submit, "Renamed Queue")
    await modal.on_submit(interaction)

    assert party.get(1, lobby.lobby_id).expires_at > soon


# -- Owner-requested UX cleanup pass (post-review-2) -------------------------


async def test_start_queue_returning_organizer_creates_immediately(party_repos, tmp_settings):
    from utils.party import PlayerPreferences

    party, *_ = party_repos
    party.set_player_preferences(1, 100, PlayerPreferences("support", "solo", True, False))
    interaction = _interaction(user_id=100)

    await bot.party_lobby_service._start_queue_after_name(interaction, "Late Night Inhouses")

    lobbies = party.recover_active(1)
    assert len(lobbies) == 1
    lobby = lobbies[0].lobby
    assert lobby.display_name == "Late Night Inhouses"
    assert lobby.capacity == 10
    assert lobby.mode == "conquest"
    organizer = lobby.participant(100)
    assert organizer.primary_role == "support"
    # No role wizard was shown — straight to the created-queue confirmation
    # with the small recruiting-card action set.
    kwargs = interaction.response.send_message.await_args.kwargs
    action_ids = {item.action for item in kwargs["view"].children}
    assert action_ids == {"join", "leave", "queue_settings", "cancel"}


async def test_start_queue_first_time_organizer_requires_role_selection(
    party_repos, tmp_settings
):
    party, *_ = party_repos
    interaction = _interaction(user_id=101)

    await bot.party_lobby_service._start_queue_after_name(interaction, "")

    # No queue created yet — the organizer must pick roles first, same as a
    # first-time joining player would.
    assert party.recover_active(1) == []
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert [item.custom_id for item in view.children] == [
        "godforge:lobby:join:primary:v2",
        "godforge:lobby:join:secondary:v2",
        "godforge:lobby:join:fill:v2",
        "godforge:lobby:join:confirm:v2",
        "godforge:lobby:join:cancel:v2",
    ]

    # Completing that lightweight wizard actually creates the queue.
    view.state = {"primary_role": "mid", "secondary_role": "", "fill": True}
    confirm_button = next(
        item for item in view.children if item.custom_id == "godforge:lobby:join:confirm:v2"
    )
    confirm_interaction = _interaction(user_id=101)
    await confirm_button.callback(confirm_interaction)

    lobbies = party.recover_active(1)
    assert len(lobbies) == 1
    organizer = lobbies[0].lobby.participant(101)
    assert organizer.primary_role == "mid"
    assert organizer.fill is True


async def test_ready_check_start_does_not_send_a_notification_ping(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    interaction = _interaction(user_id=1, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "ready_check")

    send_kwargs = interaction.channel.send.await_args.kwargs
    # Issue #63 follow-up: starting a ready check must not itself ping the
    # roster — only the final match-ready handoff does.
    assert not send_kwargs.get("content")
    embed = send_kwargs["embed"]
    assert any(field.name == "Waiting on" for field in embed.fields)


async def test_public_card_shows_compact_role_context(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    party.save_participant(
        1, lobby.lobby_id,
        Participant(1, primary_role="support", secondary_role="solo", fill=True),
        operation_id="j1",
    )
    lobby = party.save_participant(
        1, lobby.lobby_id, Participant(2, fill=True), operation_id="j2",
    )
    guild = _guild(
        1, members={1: MagicMock(display_name="Dustin"), 2: MagicMock(display_name="Debo")}
    )

    embed = bot.party_lobby_service.lobby_card_embed(lobby, guild)

    roster_field = next(field for field in embed.fields if field.name.startswith("Roster"))
    assert "Dustin · Support / Solo (Fill)" in roster_field.value
    assert "Debo · Fill" in roster_field.value


async def test_public_card_roster_truncates_with_others_count(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    for user_id in range(1, 9):
        lobby = party.save_participant(
            1, lobby.lobby_id, Participant(user_id, primary_role="mid"),
            operation_id=f"j{user_id}",
        )

    embed = bot.party_lobby_service.lobby_card_embed(lobby)

    roster_field = next(field for field in embed.fields if field.name.startswith("Roster"))
    assert roster_field.value.count("\n") == 6
    assert "+2 others" in roster_field.value


async def test_first_time_join_succeeds_without_touching_secondary_role(party_repos):
    # Issue #63 micro-fix: Secondary role is optional and defaults to None,
    # so Join must succeed end-to-end with only Primary + Fill answered.
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))

    await bot._handle_lobby_card_action(interaction, "join")

    view = interaction.response.send_message.await_args.kwargs["view"]
    view.state["primary_role"] = "adc"
    view.state["fill"] = True  # Secondary role deliberately left untouched.
    confirm_button = next(
        item for item in view.children if item.custom_id == "godforge:lobby:join:confirm:v2"
    )
    confirm_interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))

    await confirm_button.callback(confirm_interaction)

    participant = party.get(1, lobby.lobby_id).participant(2)
    assert participant.primary_role == "adc"
    assert participant.secondary_role is None
    assert participant.fill is True


# -- Half-Shell review follow-up (PR #64, POOL-002) --------------------------
#
# Restart recovery does not auto-resume a lobby that crashed between room
# provisioning succeeding and the FORMING transition — a player pressing
# Ready again self-heals it, and auto-completing during recovery with no
# live Discord interaction to author the response would add more risk than
# it removes. These tests pin that this is the current, intentional
# behavior (not an accident) and that the stuck state is at least logged.


async def test_restart_recovery_does_not_auto_complete_a_stuck_ready_check(
    party_repos, monkeypatch
):
    from utils.lifecycle import LifecycleContext

    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    # Simulates a crash after room provisioning succeeded but before the
    # lobby transitioned to FORMING: rooms already exist for this lobby.
    room_service = MagicMock()
    room_service.get = AsyncMock(return_value=MagicMock(text_room_id=555))
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    mock_log = MagicMock()
    monkeypatch.setattr(bot._party_lobby_deps, "log", mock_log)
    guild = _guild(1)
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None

    await bot.party_lobby_service.recover_match_controls(ctx)

    # Pins the current, intentional limitation: recovery does not
    # auto-transition the lobby out of READY_CHECK.
    assert party.get(1, lobby.lobby_id).state is LobbyState.READY_CHECK
    # But the stuck state is now observable rather than silent.
    mock_log.warning.assert_called_once()
    logged_args = mock_log.warning.call_args.args
    assert lobby.lobby_id in logged_args


async def test_restart_recovery_does_not_warn_for_an_ordinary_ready_check(
    party_repos, monkeypatch
):
    from utils.lifecycle import LifecycleContext

    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    # No rooms have been provisioned yet — this is the ordinary, common case
    # (most ready checks recover mid-flight, not mid-crash-after-provision).
    room_service = MagicMock()
    room_service.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )
    mock_log = MagicMock()
    monkeypatch.setattr(bot._party_lobby_deps, "log", mock_log)
    guild = _guild(1)
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None

    await bot.party_lobby_service.recover_match_controls(ctx)

    assert party.get(1, lobby.lobby_id).state is LobbyState.READY_CHECK
    mock_log.warning.assert_not_called()


# -- Issue #66: recruiting inactivity confirmation + terminal card cleanup ---


async def test_cancel_queue_deletes_the_card_instead_of_relabeling_it(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    card_message = MagicMock()
    card_message.delete = AsyncMock()
    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(return_value=card_message)
    guild = _guild(1, channels={600: public_channel})
    interaction = _interaction(
        user_id=1, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot._handle_lobby_card_action(interaction, "cancel")

    assert party.get(1, lobby.lobby_id).state is LobbyState.CANCELLED
    card_message.delete.assert_awaited_once()
    changed_delivery = party.get(1, lobby.lobby_id).delivery
    assert changed_delivery.panel_channel_id is None
    assert changed_delivery.panel_message_id is None
    reply = interaction.response.send_message.await_args.args[0]
    assert "cancelled" in reply.lower()


async def test_keep_queue_open_resets_the_clock_and_deletes_the_stale_prompt(
    party_repos,
):
    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 700, 800, operation_id="prompt-1",
    )
    prompt_message = MagicMock()
    prompt_message.delete = AsyncMock()
    prompt_channel = MagicMock(id=700)
    prompt_channel.fetch_message = AsyncMock(return_value=prompt_message)
    guild = _guild(1, channels={700: prompt_channel})
    interaction = _interaction(
        user_id=1, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot.party_lobby_service.handle_inactivity_prompt_action(interaction, "keep_open")

    changed = party.get(1, lobby.lobby_id)
    assert changed.state is LobbyState.OPEN
    assert changed.delivery.inactivity_prompt_channel_id is None
    assert changed.delivery.inactivity_prompt_message_id is None
    assert changed.expires_at > datetime.now(timezone.utc)
    prompt_message.delete.assert_awaited_once()
    reply = interaction.response.send_message.await_args.args[0]
    assert "kept open" in reply.lower()


async def test_close_queue_from_prompt_cancels_and_deletes_both_messages(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 700, 800, operation_id="prompt-1",
    )
    card_message = MagicMock()
    card_message.delete = AsyncMock()
    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(return_value=card_message)
    prompt_message = MagicMock()
    prompt_message.delete = AsyncMock()
    prompt_channel = MagicMock(id=700)
    prompt_channel.fetch_message = AsyncMock(return_value=prompt_message)
    guild = _guild(1, channels={600: public_channel, 700: prompt_channel})
    interaction = _interaction(
        user_id=1, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot.party_lobby_service.handle_inactivity_prompt_action(interaction, "close_queue")

    assert party.get(1, lobby.lobby_id).state is LobbyState.CANCELLED
    card_message.delete.assert_awaited_once()
    prompt_message.delete.assert_awaited_once()
    reply = interaction.response.send_message.await_args.args[0]
    assert "closed" in reply.lower()


async def test_only_the_organizer_can_respond_to_the_inactivity_prompt(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 700, 800, operation_id="prompt-1",
    )
    interaction = _interaction(
        user_id=2, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot.party_lobby_service.handle_inactivity_prompt_action(interaction, "close_queue")

    assert party.get(1, lobby.lobby_id).state is LobbyState.OPEN
    reply = interaction.response.send_message.await_args.args[0]
    assert "organizer" in reply.lower()


async def test_joining_during_grace_period_implicitly_reprimes_and_clears_the_prompt(
    party_repos,
):
    # Issue #66: any meaningful recruiting activity during the grace period
    # counts as implicit confirmation — the organizer never needs to click
    # Keep Queue Open, and the now-stale prompt is cleaned up automatically.
    party, *_ = party_repos
    from utils.party import PlayerPreferences

    party.set_player_preferences(1, 2, PlayerPreferences("support", "solo", True, False))
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 700, 800, operation_id="prompt-1",
    )
    prompt_message = MagicMock()
    prompt_message.delete = AsyncMock()
    prompt_channel = MagicMock(id=700)
    prompt_channel.fetch_message = AsyncMock(return_value=prompt_message)
    guild = _guild(1, channels={700: prompt_channel})
    interaction = _interaction(
        user_id=2, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )

    await bot._handle_lobby_card_action(interaction, "join")

    changed = party.get(1, lobby.lobby_id)
    assert any(p.user_id == 2 for p in changed.participants)
    assert changed.delivery.inactivity_prompt_channel_id is None
    assert changed.delivery.inactivity_prompt_message_id is None
    prompt_message.delete.assert_awaited_once()


async def test_ready_check_timeout_cancellation_deletes_the_public_card(party_repos):
    from utils.lifecycle import LifecycleContext

    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="j1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j2")
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    # Force the ready-check deadline into the past directly, rather than
    # waiting on the real 60-second timeout the fixture's service is
    # configured with.
    queue = await queue_service.get(lobby.lobby_id)
    queue.ready_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await queue_service._repository.save(queue)

    card_message = MagicMock()
    card_message.delete = AsyncMock()
    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(return_value=card_message)
    guild = _guild(1, channels={600: public_channel})
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None
    ctx.get_channel = lambda channel_id: None

    await bot.party_lobby_service.expire_ready_checks(ctx)

    assert party.get(1, lobby.lobby_id).state is LobbyState.CANCELLED
    card_message.delete.assert_awaited_once()


async def test_periodic_sweep_self_heals_a_card_orphaned_by_a_crash(party_repos):
    # Issue #66 restart-safety: a crash between the CANCELLED/EXPIRED
    # transition committing and its card-deletion follow-up completing would
    # otherwise leave a dead card behind forever. The periodic sweep finds
    # and finishes that cleanup on its own, with no dedicated on-startup
    # reconciliation needed.
    from utils.lifecycle import LifecycleContext

    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=4, operation_id="create-1")
    party.set_delivery(
        1, lobby.lobby_id, DiscordDelivery(panel_channel_id=600, panel_message_id=601),
        operation_id="delivery",
    )
    party.transition(1, lobby.lobby_id, LobbyState.CANCELLED, operation_id="cancel-1")

    card_message = MagicMock()
    card_message.delete = AsyncMock()
    public_channel = MagicMock(id=600)
    public_channel.fetch_message = AsyncMock(return_value=card_message)
    guild = _guild(1, channels={600: public_channel})
    ctx = MagicMock(spec=LifecycleContext)
    ctx.get_guild = lambda guild_id: guild if guild_id == 1 else None
    ctx.get_channel = lambda channel_id: None

    await bot.party_lobby_service.expire_ready_checks(ctx)

    card_message.delete.assert_awaited_once()
    changed_delivery = party.get(1, lobby.lobby_id).delivery
    assert changed_delivery.panel_channel_id is None
    assert changed_delivery.panel_message_id is None


async def test_periodic_sweep_does_not_post_a_second_prompt_once_one_exists(
    party_repos,
):
    party, *_ = party_repos
    lobby = party.create(
        guild_id=1, organizer_id=1, capacity=4, operation_id="create-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    party.record_inactivity_prompt_sent(
        1, lobby.lobby_id, 700, 800, operation_id="prompt-1",
    )

    due = party.due_for_inactivity_prompt(1)

    assert due == []
