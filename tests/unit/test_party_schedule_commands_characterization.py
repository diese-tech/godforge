"""Characterization tests for the scheduled-night /party commands in bot.py.

Written before extracting these handlers into a feature module (Issue #48,
Phase 7b). Pins down current behavior of bot.party_schedule, party_confirm,
party_rsvp, party_unrsvp, party_events, party_calendar, and
party_open_scheduled, which previously had no direct test coverage — only the
underlying utils.party_schedule domain logic was tested. Interactions are
mocked; app_commands.Command objects are invoked via .callback.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService
from utils.party_schedule import ScheduleRepository
from utils.party_store import SQLitePartyRepository

FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
WHEN = FUTURE.strftime("%Y-%m-%d %H:%M")


@pytest.fixture()
def schedule_repos(tmp_path, monkeypatch):
    schedule = ScheduleRepository(tmp_path / "party.db")
    party = SQLitePartyRepository(tmp_path / "party.db")
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    monkeypatch.setattr(bot, "schedule_repository", schedule)
    monkeypatch.setattr(bot, "party_repository", party)
    monkeypatch.setattr(bot, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._schedule_deps, "schedule_repository", schedule)
    monkeypatch.setattr(bot._schedule_deps, "party_repository", party)
    monkeypatch.setattr(bot._schedule_deps, "party_queue_service", queue_service)
    return schedule, party, queue_service


def _interaction(*, guild_id=1, user_id=100):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    # None (not an auto-generated truthy MagicMock) so the Issue #67
    # card-refresh calls these handlers now make are no-ops here — this
    # file characterizes command-level behavior; card behavior has its own
    # dedicated tests in test_schedule_rsvp_cards.py.
    interaction.guild = None
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_schedule_creates_pending_event(schedule_repos):
    schedule, *_ = schedule_repos
    interaction = _interaction(user_id=1)
    await bot.party_schedule.callback(
        interaction,
        title="Friday Night",
        when=WHEN,
        timezone_name="America/New_York",
        recurrence="once",
        capacity=10,
        role_slots="",
        reminders="60,15",
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert "Confirm" in reply
    assert "Friday Night" in reply


async def test_schedule_requires_guild(schedule_repos):
    interaction = _interaction(guild_id=None)
    await bot.party_schedule.callback(
        interaction,
        title="T",
        when=WHEN,
        timezone_name="America/New_York",
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert "Server-only" in reply


def _pending_event(schedule, *, organizer_id=1):
    return schedule.create(
        guild_id=1,
        organizer_id=organizer_id,
        title="Friday Night",
        starts_at=FUTURE,
        timezone_name="America/New_York",
        recurrence="once",
        capacity=10,
        role_slots=(),
        reminder_minutes=(60, 15),
        operation_id="op-1",
    )


async def test_confirm_publishes_event(schedule_repos):
    schedule, *_ = schedule_repos
    event = _pending_event(schedule)
    interaction = _interaction(user_id=1)
    await bot.party_confirm.callback(interaction, event_id=event.event_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Scheduled" in reply
    assert "Friday Night" in reply


async def test_confirm_wrong_organizer_reports_error(schedule_repos):
    schedule, *_ = schedule_repos
    event = _pending_event(schedule)
    interaction = _interaction(user_id=999)
    await bot.party_confirm.callback(interaction, event_id=event.event_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "organizer" in reply.lower()


def _confirmed_event(schedule, *, organizer_id=1):
    event = _pending_event(schedule, organizer_id=organizer_id)
    return schedule.confirm(event.event_id, organizer_id)


async def test_rsvp_reserves_seat(schedule_repos):
    schedule, *_ = schedule_repos
    event = _confirmed_event(schedule)
    interaction = _interaction(user_id=2)
    await bot.party_rsvp.callback(interaction, event_id=event.event_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Seat reserved" in reply


async def test_rsvp_requires_guild(schedule_repos):
    interaction = _interaction(guild_id=None)
    await bot.party_rsvp.callback(interaction, event_id="missing")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Server-only" in reply


async def test_rsvp_missing_event_reports_not_found(schedule_repos):
    interaction = _interaction()
    await bot.party_rsvp.callback(interaction, event_id="missing")
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" in reply.lower()


async def test_unrsvp_releases_reservation(schedule_repos):
    schedule, *_ = schedule_repos
    event = _confirmed_event(schedule)
    from utils.party import PlayerPreferences

    schedule.rsvp(event.event_id, 2, PlayerPreferences())
    interaction = _interaction(user_id=2)
    await bot.party_unrsvp.callback(interaction, event_id=event.event_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "released" in reply.lower()


async def test_events_lists_upcoming(schedule_repos):
    schedule, *_ = schedule_repos
    _confirmed_event(schedule)
    interaction = _interaction()
    await bot.party_events.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Friday Night" in reply


async def test_events_empty_reports_none_scheduled(schedule_repos):
    interaction = _interaction()
    await bot.party_events.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "No custom nights" in reply


async def test_calendar_sends_ics_file(schedule_repos):
    schedule, *_ = schedule_repos
    event = _confirmed_event(schedule)
    interaction = _interaction()
    await bot.party_calendar.callback(interaction, event_id=event.event_id)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["file"].filename == f"godforge-{event.event_id}.ics"


async def test_open_scheduled_requires_organizer(schedule_repos):
    schedule, *_ = schedule_repos
    event = _confirmed_event(schedule)
    interaction = _interaction(user_id=999)
    await bot.party_open_scheduled.callback(interaction, event_id=event.event_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_open_scheduled_missing_event_reports_not_found(schedule_repos):
    interaction = _interaction()
    await bot.party_open_scheduled.callback(interaction, event_id="missing")
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" in reply.lower()
