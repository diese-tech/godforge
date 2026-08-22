from datetime import datetime, timezone

import pytest

from utils.party import LobbyState, PlayerPreferences
from utils.party_queue import (
    InMemoryPartyQueueRepository,
    PartyQueueService,
    QueueStatus,
    SQLitePartyQueueRepository,
)
from utils.party_schedule import (
    EventState,
    Recurrence,
    ScheduleError,
    ScheduleRepository,
    calendar_ics,
    convert_to_lobby,
    parse_local_start,
)
from utils.party_store import SQLitePartyRepository


NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def test_natural_time_requires_explicit_valid_timezone_and_normalizes_dst():
    assert parse_local_start(
        "Friday 8 PM", "America/New_York", now=NOW
    ) == datetime(2026, 7, 25, 0, tzinfo=timezone.utc)
    assert parse_local_start(
        "2026-12-04 8 PM", "America/New_York", now=NOW
    ) == datetime(2026, 12, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError, match="IANA"):
        parse_local_start("tomorrow 8 PM", "EST", now=NOW)


def test_natural_time_rejects_nonexistent_and_ambiguous_dst_wall_times():
    with pytest.raises(ScheduleError, match="does not exist"):
        parse_local_start(
            "2027-03-14 2:30 AM",
            "America/New_York",
            now=NOW,
        )
    with pytest.raises(ScheduleError, match="ambiguous"):
        parse_local_start(
            "2026-11-01 1:30 AM",
            "America/New_York",
            now=NOW,
        )


def _event(repository, *, recurrence=Recurrence.ONCE, capacity=2):
    return repository.create(
        guild_id=1,
        organizer_id=10,
        title="Friday Customs",
        starts_at=datetime(2026, 7, 25, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        recurrence=recurrence,
        capacity=capacity,
        role_slots=("solo", "jungle"),
        reminder_minutes=(60, 15),
        operation_id="interaction-1",
    )


def test_confirmation_rsvp_waitlist_and_promotion_survive_restart(tmp_path):
    path = tmp_path / "party.db"
    repository = ScheduleRepository(path)
    pending = _event(repository)
    assert pending.state is EventState.PENDING_CONFIRMATION
    confirmed = repository.confirm(pending.event_id, 10)
    assert confirmed.state is EventState.SCHEDULED
    repository.rsvp(
        pending.event_id, 1, PlayerPreferences("solo", "support", captain=True)
    )
    repository.rsvp(pending.event_id, 2, PlayerPreferences("jungle"))
    full = repository.rsvp(pending.event_id, 3, PlayerPreferences("mid", fill=True))
    assert [r.user_id for r in full.rsvps] == [1, 2]
    assert [r.user_id for r in full.waitlist] == [3]

    promoted = ScheduleRepository(path).cancel_rsvp(pending.event_id, 1)
    assert [r.user_id for r in promoted.rsvps] == [2, 3]
    assert promoted.rsvps[1].preferences.fill is True


def test_repeat_rsvp_for_an_already_going_player_updates_roles_in_place(tmp_path):
    # Issue #67's "Change Roles" flow re-submits rsvp() for a player who
    # never left Going — their queue position must stay put, but the stored
    # role preferences must actually change (a Codex review on PR #70 caught
    # the original no-op-on-repeat-call behavior silently discarding this).
    repository = ScheduleRepository(tmp_path / "party.db")
    event = _event(repository, capacity=10)
    repository.confirm(event.event_id, 10)
    repository.rsvp(event.event_id, 1, PlayerPreferences("solo"))
    repository.rsvp(event.event_id, 2, PlayerPreferences("jungle"))

    changed = repository.rsvp(event.event_id, 1, PlayerPreferences("support", "mid", fill=True))

    assert [r.user_id for r in changed.rsvps] == [1, 2]  # position unchanged
    assert changed.rsvps[0].preferences.primary_role == "support"
    assert changed.rsvps[0].preferences.secondary_role == "mid"
    assert changed.rsvps[0].preferences.fill is True


@pytest.mark.asyncio
async def test_conversion_is_idempotent_and_preserves_preferences_and_waitlist(tmp_path):
    path = tmp_path / "party.db"
    schedules = ScheduleRepository(path)
    event = _event(schedules)
    schedules.confirm(event.event_id, 10)
    schedules.rsvp(event.event_id, 1, PlayerPreferences("solo", captain=True))
    schedules.rsvp(event.event_id, 2, PlayerPreferences("jungle"))
    schedules.rsvp(event.event_id, 3, PlayerPreferences("support", fill=True))
    parties = SQLitePartyRepository(path)
    queues = PartyQueueService(InMemoryPartyQueueRepository())

    first = await convert_to_lobby(schedules.get(event.event_id), schedules, parties, queues)
    first_queue = await queues.get(first.lobby_id)
    second = await convert_to_lobby(schedules.get(event.event_id), schedules, parties, queues)

    assert first.lobby_id == second.lobby_id == f"scheduled-{event.event_id}"
    assert first.participant(1).primary_role == "solo"
    assert first.participant(1).captain is True
    queue = await queues.get(first.lobby_id)
    assert [member.user_id for member in queue.active] == [1, 2]
    assert [member.user_id for member in queue.waitlist] == [3]
    assert first.state is second.state is LobbyState.READY_CHECK
    assert queue.status is QueueStatus.READY_CHECK
    assert queue.ready_deadline == first_queue.ready_deadline
    assert schedules.get(event.event_id).state is EventState.CONVERTED


def test_weekly_calendar_export_needs_no_calendar_account(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = _event(repository, recurrence=Recurrence.WEEKLY)
    payload = calendar_ics(event).decode()
    assert "BEGIN:VCALENDAR" in payload
    assert "RRULE:FREQ=WEEKLY" in payload
    assert "DTSTART;TZID=America/New_York:20260724T200000" in payload
    assert f"UID:{event.event_id}@godforge" in payload


def test_weekly_calendar_keeps_wall_time_across_dst(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = repository.create(
        guild_id=1,
        organizer_id=10,
        title="Saturday Customs",
        starts_at=datetime(2026, 11, 1, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        recurrence=Recurrence.WEEKLY,
        capacity=10,
        operation_id="dst-ics",
    )
    payload = calendar_ics(event).decode()
    assert "DTSTART;TZID=America/New_York:20261031T200000" in payload
    assert "RRULE:FREQ=WEEKLY" in payload


def test_weekly_conversion_schedules_next_occurrence_once(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = _event(repository, recurrence=Recurrence.WEEKLY)
    repository.confirm(event.event_id, 10)
    repository.mark_converted(event.event_id, "lobby-one")
    repository.mark_converted(event.event_id, "lobby-one")
    upcoming = repository.list_upcoming(1, now=NOW)
    assert len(upcoming) == 1
    assert upcoming[0].starts_at == datetime(2026, 8, 1, 0, tzinfo=timezone.utc)


def test_weekly_successor_preserves_local_wall_time_across_dst(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = repository.create(
        guild_id=1,
        organizer_id=10,
        title="Saturday Customs",
        starts_at=datetime(2026, 11, 1, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        recurrence=Recurrence.WEEKLY,
        capacity=10,
        operation_id="dst-week",
    )
    repository.confirm(event.event_id, 10)
    repository.mark_converted(event.event_id, "dst-lobby")

    successor = repository.list_upcoming(
        1, now=datetime(2026, 10, 31, tzinfo=timezone.utc)
    )[0]
    assert successor.starts_at == datetime(2026, 11, 8, 1, tzinfo=timezone.utc)


def test_create_retry_and_confirm_are_idempotent(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    first = _event(repository)
    second = _event(repository)
    assert first.event_id == second.event_id
    repository.confirm(first.event_id, 10)
    assert repository.confirm(first.event_id, 10).state is EventState.SCHEDULED


def test_reminder_claim_is_restart_safe_and_only_due_before_start(tmp_path):
    path = tmp_path / "party.db"
    repository = ScheduleRepository(path)
    event = _event(repository)
    repository.confirm(event.event_id, 10)
    due_at = datetime(2026, 7, 24, 23, 1, tzinfo=timezone.utc)
    claimed = repository.claim_due_reminders(now=due_at)
    assert [(minutes, occurrence) for _, minutes, occurrence in claimed] == [
        (60, event.starts_at)
    ]
    assert ScheduleRepository(path).claim_due_reminders(now=due_at) == []


def test_weekly_reminder_preserves_wall_time_across_dst(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = repository.create(
        guild_id=1,
        organizer_id=10,
        title="Saturday Customs",
        starts_at=datetime(2026, 11, 1, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        recurrence=Recurrence.WEEKLY,
        capacity=10,
        reminder_minutes=(60,),
        operation_id="dst-reminder",
    )
    repository.confirm(event.event_id, 10)
    claimed = repository.claim_due_reminders(
        now=datetime(2026, 11, 8, 0, 1, tzinfo=timezone.utc)
    )
    assert [(minutes, occurrence) for _, minutes, occurrence in claimed] == [
        (60, datetime(2026, 11, 8, 1, tzinfo=timezone.utc))
    ]


def test_reminders_shorter_than_poll_interval_are_rejected(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    with pytest.raises(ScheduleError, match="5-10080"):
        repository.create(
            guild_id=1,
            organizer_id=10,
            title="Too Late",
            starts_at=datetime(2026, 7, 25, 0, tzinfo=timezone.utc),
            timezone_name="America/New_York",
            recurrence=Recurrence.ONCE,
            capacity=10,
            reminder_minutes=(4,),
            operation_id="short-reminder",
        )


@pytest.mark.asyncio
async def test_full_roster_conversion_recovers_ready_check_after_restart(tmp_path):
    path = tmp_path / "party.db"
    schedules = ScheduleRepository(path)
    event = _event(schedules)
    schedules.confirm(event.event_id, 10)
    schedules.rsvp(event.event_id, 1, PlayerPreferences("solo"))
    schedules.rsvp(event.event_id, 2, PlayerPreferences("jungle"))
    parties = SQLitePartyRepository(path)
    queues = PartyQueueService(SQLitePartyQueueRepository(path))
    first = await convert_to_lobby(
        schedules.get(event.event_id), schedules, parties, queues
    )
    first_queue = await queues.get(first.lobby_id)

    recovered = await convert_to_lobby(
        ScheduleRepository(path).get(event.event_id),
        ScheduleRepository(path),
        SQLitePartyRepository(path),
        PartyQueueService(SQLitePartyQueueRepository(path)),
    )
    recovered_queue = await SQLitePartyQueueRepository(path).load(first.lobby_id)

    assert recovered.state is LobbyState.READY_CHECK
    assert recovered_queue.status is QueueStatus.READY_CHECK
    assert recovered_queue.ready_deadline == first_queue.ready_deadline
