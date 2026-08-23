"""Regression coverage for the Issue #67 scheduled-night RSVP card.

Exercises the real ``ScheduleRepository``/``SQLitePartyRepository`` with
mocked Discord interactions/channels/guilds, scoped to what #67 introduced:

- auto-publish on confirm (no separate Share step)
- returning-player fast RSVP vs first-time role wizard
- Maybe (never occupies a seat; releases one if held) and Can't Make It
- every RSVP mutation refreshing the canonical card via its delivery ref,
  never whichever message triggered the interaction
- reconciliation: post-if-missing, repost-if-deleted, no duplicate if live
- the periodic/startup sweep across every guild
- the admin `/party session-refresh` recovery command
- weekly rollover keeping one Discord message across occurrences
- multi-session isolation
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot
from utils.party import PlayerPreferences
from utils.party_schedule import EventState, Recurrence, ScheduleRepository
from utils.party_store import SQLitePartyRepository

FUTURE = datetime.now(timezone.utc) + timedelta(days=7)


@pytest.fixture()
def schedule_repos(tmp_path, monkeypatch, tmp_settings):
    schedule = ScheduleRepository(tmp_path / "party.db")
    party = SQLitePartyRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "schedule_repository", schedule)
    monkeypatch.setattr(bot, "party_repository", party)
    monkeypatch.setattr(bot._schedule_deps, "schedule_repository", schedule)
    monkeypatch.setattr(bot._schedule_deps, "party_repository", party)
    monkeypatch.setattr(bot._schedule_rsvp_deps, "schedule_repository", schedule)
    monkeypatch.setattr(bot._schedule_rsvp_deps, "party_repository", party)
    monkeypatch.setattr(bot.schedule_lifecycle, "_schedule_repository", schedule)
    return schedule, party


def _guild(guild_id=1, *, channels=None):
    guild = MagicMock()
    guild.id = guild_id
    guild.get_member = lambda uid: None
    guild.get_channel = lambda cid: (channels or {}).get(cid)
    return guild


def _channel(channel_id, *, fetch_returns=None, fetch_raises=None):
    channel = MagicMock(id=channel_id)
    channel.send = AsyncMock(return_value=MagicMock(id=channel_id * 10))
    if fetch_raises is not None:
        channel.fetch_message = AsyncMock(side_effect=fetch_raises)
    else:
        message = fetch_returns or MagicMock()
        message.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=message)
    return channel


def _interaction(*, guild_id=1, user_id=100, guild=None, message=None):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = MagicMock(id=user_id)
    interaction.user.guild_permissions.manage_guild = False
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.message = message
    return interaction


def _rsvp_footer_message(event_id):
    footer = MagicMock(text=f"event_id={event_id}")
    embed = MagicMock(footer=footer)
    message = MagicMock(embeds=[embed])
    return message


def _confirmed_event(schedule, *, organizer_id=1, recurrence=Recurrence.ONCE, capacity=2):
    event = schedule.create(
        guild_id=1, organizer_id=organizer_id, title="Friday Night",
        starts_at=FUTURE, timezone_name="America/New_York",
        recurrence=recurrence, capacity=capacity, operation_id=f"op-{organizer_id}-{recurrence}",
    )
    return schedule.confirm(event.event_id, organizer_id)


# -- 1. Auto-publish on confirm ----------------------------------------------


async def test_confirm_auto_publishes_the_rsvp_card(schedule_repos):
    import utils.settings as settings_mod

    schedule, _ = schedule_repos
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    play_channel = _channel(700)
    guild = _guild(1, channels={700: play_channel})
    event = schedule.create(
        guild_id=1, organizer_id=1, title="Friday Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-confirm",
    )
    interaction = _interaction(user_id=1, guild=guild)

    await bot.party_confirm.callback(interaction, event_id=event.event_id)

    play_channel.send.assert_awaited_once()
    kwargs = play_channel.send.await_args.kwargs
    assert kwargs["embed"].title == "Friday Night"
    assert "Going · 0/10" in str(kwargs["embed"].to_dict())
    changed = schedule.get(event.event_id)
    assert changed.delivery_channel_id == 700
    assert changed.delivery_message_id == 7000


# -- 2. Returning vs first-time RSVP -----------------------------------------


async def test_returning_player_going_rsvps_instantly_and_refreshes_card(schedule_repos):
    schedule, party = schedule_repos
    party.set_player_preferences(1, 5, PlayerPreferences("support", "solo", True, False))
    event = _confirmed_event(schedule, capacity=10)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    schedule.set_delivery(event.event_id, 600, 601)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(
        user_id=5, guild=guild, message=_rsvp_footer_message(event.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "going")

    changed = schedule.get(event.event_id)
    assert any(r.user_id == 5 for r in changed.rsvps)
    card_message.edit.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs.get("view") is not None
    ack = interaction.response.send_message.await_args.args[0]
    assert "Going" in ack and "support/solo" in ack


async def test_first_time_going_shows_role_wizard_then_rsvps_on_submit(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    schedule.set_delivery(event.event_id, 600, 601)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(
        user_id=9, guild=guild, message=_rsvp_footer_message(event.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "going")

    kwargs = interaction.response.send_message.await_args.kwargs
    wizard = kwargs["view"]
    custom_ids = [item.custom_id for item in wizard.children]
    assert "godforge:lobby:join:primary:v2" in custom_ids
    # No RSVP was recorded yet — only after the wizard is submitted.
    assert schedule.get(event.event_id).rsvps == ()

    wizard.state["primary_role"] = "mid"
    wizard.state["fill"] = False
    wizard_interaction = _interaction(
        user_id=9, guild=guild, message=_rsvp_footer_message(event.event_id)
    )
    await wizard.act(wizard_interaction, "confirm")

    changed = schedule.get(event.event_id)
    assert changed.rsvps[0].user_id == 9
    assert changed.rsvps[0].preferences.primary_role == "mid"
    saved = party.get_player_preferences(1, 9)
    assert saved.primary_role == "mid"
    card_message.edit.assert_awaited_once()


# -- 3. Maybe and Can't Make It -----------------------------------------------


async def test_maybe_records_without_a_wizard_and_never_occupies_a_seat(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    guild = _guild(1)
    interaction = _interaction(
        user_id=3, guild=guild, message=_rsvp_footer_message(event.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "maybe")

    changed = schedule.get(event.event_id)
    assert [r.user_id for r in changed.maybe] == [3]
    assert changed.rsvps == ()
    reply = interaction.response.send_message.await_args.args[0]
    assert "Maybe" in reply


async def test_maybe_releases_a_held_seat_and_promotes_the_waitlist(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=2)
    schedule.rsvp(event.event_id, 0, PlayerPreferences("support"))
    schedule.rsvp(event.event_id, 1, PlayerPreferences("solo"))
    schedule.rsvp(event.event_id, 2, PlayerPreferences("mid"))
    assert [r.user_id for r in schedule.get(event.event_id).waitlist] == [2]
    guild = _guild(1)
    interaction = _interaction(
        user_id=1, guild=guild, message=_rsvp_footer_message(event.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "maybe")

    changed = schedule.get(event.event_id)
    assert [r.user_id for r in changed.maybe] == [1]
    assert [r.user_id for r in changed.rsvps] == [0, 2]
    assert changed.waitlist == ()


async def test_cant_make_it_removes_either_going_or_maybe_response(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    schedule.rsvp(event.event_id, 4, PlayerPreferences("adc"))
    guild = _guild(1)
    interaction = _interaction(
        user_id=4, guild=guild, message=_rsvp_footer_message(event.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "cant_make_it")

    changed = schedule.get(event.event_id)
    assert changed.rsvps == ()
    assert changed.maybe == ()
    reply = interaction.response.send_message.await_args.args[0]
    assert "removed" in reply.lower()


# -- 4. Mutations always refresh the durable card, never the trigger --------


async def test_rsvp_via_slash_command_still_refreshes_the_public_card(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    schedule.set_delivery(event.event_id, 600, 601)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(user_id=7, guild=guild)

    await bot.party_rsvp.callback(interaction, event_id=event.event_id)

    card_message.edit.assert_awaited_once()
    refreshed_embed = card_message.edit.await_args.kwargs["embed"]
    assert "1/10" in str(refreshed_embed.to_dict())


# -- 5. Reconciliation: post / repost-if-deleted / no duplicate -------------


async def test_reconcile_posts_a_card_for_a_session_with_none(schedule_repos):
    import utils.settings as settings_mod

    schedule, party = schedule_repos
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    event = _confirmed_event(schedule, capacity=10)
    channel = _channel(700)
    guild = _guild(1, channels={700: channel})

    await bot.schedule_rsvp_service.reconcile(event, MagicMock(get_guild=lambda gid: guild))

    channel.send.assert_awaited_once()
    assert schedule.get(event.event_id).delivery_message_id == 7000


async def test_reconcile_reposts_when_the_stored_message_was_deleted(schedule_repos):
    import utils.settings as settings_mod

    schedule, party = schedule_repos
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    event = _confirmed_event(schedule, capacity=10)
    schedule.set_delivery(event.event_id, 600, 601)
    stale_channel = _channel(600, fetch_raises=discord.NotFound(MagicMock(), "gone"))
    play_channel = _channel(700)
    guild = _guild(1, channels={600: stale_channel, 700: play_channel})

    await bot.schedule_rsvp_service.reconcile(
        schedule.get(event.event_id), MagicMock(get_guild=lambda gid: guild)
    )

    play_channel.send.assert_awaited_once()
    changed = schedule.get(event.event_id)
    assert changed.delivery_channel_id == 700
    assert changed.delivery_message_id == 7000


async def test_reconcile_does_not_duplicate_a_still_live_card(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    schedule.set_delivery(event.event_id, 600, 601)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    guild = _guild(1, channels={600: channel})

    await bot.schedule_rsvp_service.reconcile(
        schedule.get(event.event_id), MagicMock(get_guild=lambda gid: guild)
    )

    channel.send.assert_not_awaited()
    card_message.edit.assert_awaited_once()


async def test_reconcile_skips_a_night_not_yet_confirmed(schedule_repos):
    schedule, party = schedule_repos
    event = schedule.create(
        guild_id=1, organizer_id=1, title="Pending", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-pending",
    )
    channel = _channel(700)
    guild = _guild(1, channels={700: channel})

    await bot.schedule_rsvp_service.reconcile(event, MagicMock(get_guild=lambda gid: guild))

    channel.send.assert_not_awaited()


# -- 6. Periodic/startup sweep across every guild -----------------------------


async def test_lifecycle_sweep_reconciles_every_upcoming_session_across_guilds(schedule_repos):
    from utils.lifecycle import LifecycleContext

    schedule, party = schedule_repos
    first = schedule.create(
        guild_id=1, organizer_id=1, title="Guild One Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-g1",
    )
    schedule.confirm(first.event_id, 1)
    second = schedule.create(
        guild_id=2, organizer_id=1, title="Guild Two Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-g2",
    )
    schedule.confirm(second.event_id, 1)
    channel_a = _channel(700)
    channel_b = _channel(800)
    guild_a = _guild(1, channels={700: channel_a})
    guild_b = _guild(2, channels={800: channel_b})

    import utils.settings as settings_mod
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    settings_mod.update_guild_settings("2", {"managed": {"playChannelId": "800"}})

    ctx = LifecycleContext(get_guild=lambda gid: {1: guild_a, 2: guild_b}.get(gid))
    await bot.schedule_lifecycle.on_startup(ctx)

    channel_a.send.assert_awaited_once()
    channel_b.send.assert_awaited_once()
    assert schedule.get(first.event_id).delivery_message_id is not None
    assert schedule.get(second.event_id).delivery_message_id is not None


# -- 7. Admin `/party session-refresh` recovery -------------------------------


async def test_session_refresh_requires_organizer_or_manager(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, organizer_id=1, capacity=10)
    interaction = _interaction(user_id=999, guild=_guild(1))

    await bot.party_session_refresh.callback(interaction, event_id=event.event_id)

    reply = interaction.response.send_message.await_args.args[0]
    assert "organizer" in reply.lower() or "manager" in reply.lower()


async def test_session_refresh_reconciles_without_duplicating(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, organizer_id=1, capacity=10)
    schedule.set_delivery(event.event_id, 600, 601)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(user_id=1, guild=guild)

    await bot.party_session_refresh.callback(interaction, event_id=event.event_id)

    channel.send.assert_not_awaited()
    card_message.edit.assert_awaited_once()
    reply = interaction.followup.send.await_args.args[0]
    assert "reconciled" in reply.lower()


async def test_session_refresh_reposts_a_missing_card(schedule_repos):
    import utils.settings as settings_mod

    schedule, party = schedule_repos
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    event = _confirmed_event(schedule, organizer_id=1, capacity=10)
    channel = _channel(700)
    guild = _guild(1, channels={700: channel})
    interaction = _interaction(user_id=1, guild=guild)

    await bot.party_session_refresh.callback(interaction, event_id=event.event_id)

    channel.send.assert_awaited_once()
    reply = interaction.followup.send.await_args.args[0]
    assert "posted" in reply.lower()


# -- 8. Weekly rollover keeps one Discord message ----------------------------


async def test_weekly_rollover_reuses_the_same_card_with_a_fresh_roster(schedule_repos):
    from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService

    schedule, party = schedule_repos
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    bot.party_queue_service = queue_service
    bot._schedule_deps.party_queue_service = queue_service
    event = _confirmed_event(schedule, organizer_id=1, recurrence=Recurrence.WEEKLY, capacity=10)
    schedule.rsvp(event.event_id, 1, PlayerPreferences("solo"))
    schedule.set_delivery(event.event_id, 600, 601)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(user_id=1, guild=guild)

    await bot.party_open_scheduled.callback(interaction, event_id=event.event_id)

    converted = schedule.get(event.event_id)
    assert converted.state is EventState.CONVERTED
    assert converted.delivery_channel_id is None  # transferred away, not duplicated
    successor = schedule.find_successor(event.event_id)
    assert successor is not None
    assert successor.delivery_channel_id == 600
    assert successor.delivery_message_id == 601
    assert successor.rsvps == ()  # fresh roster, no carried-over RSVPs
    assert converted.rsvps[0].user_id == 1  # historical data stays on the old row
    card_message.edit.assert_awaited_once()
    refreshed_embed = card_message.edit.await_args.kwargs["embed"]
    assert "Going · 0/10" in str(refreshed_embed.to_dict())


async def test_one_time_conversion_edits_its_own_card_to_a_terminal_state(schedule_repos):
    from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService

    schedule, party = schedule_repos
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    bot.party_queue_service = queue_service
    bot._schedule_deps.party_queue_service = queue_service
    event = _confirmed_event(schedule, organizer_id=1, recurrence=Recurrence.ONCE, capacity=10)
    schedule.set_delivery(event.event_id, 600, 601)
    card_message = MagicMock()
    card_message.edit = AsyncMock()
    channel = _channel(600, fetch_returns=card_message)
    guild = _guild(1, channels={600: channel})
    interaction = _interaction(user_id=1, guild=guild)

    await bot.party_open_scheduled.callback(interaction, event_id=event.event_id)

    assert schedule.find_successor(event.event_id) is None
    card_message.edit.assert_awaited_once()
    refreshed_embed = card_message.edit.await_args.kwargs["embed"]
    assert "session has started" in str(refreshed_embed.to_dict()).lower()
    assert card_message.edit.await_args.kwargs["view"] is None


# -- 9. Multi-session isolation ----------------------------------------------


async def test_two_sessions_in_one_guild_route_to_independent_cards(schedule_repos):
    schedule, party = schedule_repos
    first = _confirmed_event(schedule, organizer_id=1, capacity=10)
    second = schedule.create(
        guild_id=1, organizer_id=2, title="Saturday Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-second",
    )
    second = schedule.confirm(second.event_id, 2)
    first_message = MagicMock()
    first_message.edit = AsyncMock()
    first_channel = _channel(600, fetch_returns=first_message)
    second_message = MagicMock()
    second_message.edit = AsyncMock()
    second_channel = _channel(700, fetch_returns=second_message)
    schedule.set_delivery(first.event_id, 600, 601)
    schedule.set_delivery(second.event_id, 700, 701)
    guild = _guild(1, channels={600: first_channel, 700: second_channel})
    interaction = _interaction(
        user_id=3, guild=guild, message=_rsvp_footer_message(second.event_id)
    )

    await bot._handle_schedule_rsvp_action(interaction, "maybe")

    second_message.edit.assert_awaited_once()
    first_message.edit.assert_not_awaited()
    assert schedule.get(first.event_id).maybe == ()
    assert [r.user_id for r in schedule.get(second.event_id).maybe] == [3]


# -- 10. Codex-fix regressions: publish failures and unverifiable reposts ----


async def test_publish_failure_does_not_abort_the_sweep_for_other_guilds(schedule_repos):
    from utils.lifecycle import LifecycleContext

    schedule, party = schedule_repos
    import utils.settings as settings_mod

    failing = schedule.create(
        guild_id=1, organizer_id=1, title="Forbidden Channel Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-forbidden",
    )
    schedule.confirm(failing.event_id, 1)
    healthy = schedule.create(
        guild_id=2, organizer_id=1, title="Healthy Guild Night", starts_at=FUTURE,
        timezone_name="America/New_York", recurrence=Recurrence.ONCE, capacity=10,
        operation_id="op-healthy",
    )
    schedule.confirm(healthy.event_id, 1)
    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "700"}})
    settings_mod.update_guild_settings("2", {"managed": {"playChannelId": "800"}})
    forbidden_channel = _channel(700)
    forbidden_channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
    healthy_channel = _channel(800)
    guild_a = _guild(1, channels={700: forbidden_channel})
    guild_b = _guild(2, channels={800: healthy_channel})
    ctx = LifecycleContext(get_guild=lambda gid: {1: guild_a, 2: guild_b}.get(gid))

    # Must not raise: a publish failure on one guild's card must never
    # propagate out of the shared sweep and abort every other guild's
    # reconciliation (or the reminder loop that runs after it).
    await bot.schedule_lifecycle.on_startup(ctx)

    forbidden_channel.send.assert_awaited_once()
    healthy_channel.send.assert_awaited_once()
    assert schedule.get(failing.event_id).delivery_message_id is None
    assert schedule.get(healthy.event_id).delivery_message_id is not None


async def test_reconcile_does_not_repost_when_the_card_cannot_be_verified(schedule_repos):
    schedule, party = schedule_repos
    event = _confirmed_event(schedule, capacity=10)
    schedule.set_delivery(event.event_id, 600, 601)
    unverifiable_channel = _channel(
        600, fetch_raises=discord.Forbidden(MagicMock(), "missing read history")
    )
    guild = _guild(1, channels={600: unverifiable_channel})

    await bot.schedule_rsvp_service.reconcile(
        schedule.get(event.event_id), MagicMock(get_guild=lambda gid: guild)
    )

    # Forbidden/HTTPException on the existence check means "can't tell", not
    # "gone" — must not repost a duplicate. The delivery ref stays as-is so
    # the next sweep retries the verification instead of piling up cards.
    unverifiable_channel.send.assert_not_awaited()
    changed = schedule.get(event.event_id)
    assert changed.delivery_channel_id == 600
    assert changed.delivery_message_id == 601
