"""Scheduled SMITE nights that hand off into the existing party lifecycle."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.party import LobbyState, Participant, PlayerPreferences, ensure_utc
from utils.party_queue import QueueStatus


class ScheduleError(ValueError):
    pass


class Recurrence(StrEnum):
    ONCE = "once"
    WEEKLY = "weekly"


class EventState(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    SCHEDULED = "scheduled"
    CONVERTED = "converted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RSVP:
    user_id: int
    preferences: PlayerPreferences = field(default_factory=PlayerPreferences)
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ScheduledNight:
    event_id: str
    guild_id: int
    organizer_id: int
    title: str
    starts_at: datetime
    timezone_name: str
    recurrence: Recurrence
    capacity: int
    role_slots: tuple[str, ...] = ()
    reminder_minutes: tuple[int, ...] = (60, 15)
    state: EventState = EventState.PENDING_CONFIRMATION
    rsvps: tuple[RSVP, ...] = ()
    waitlist: tuple[RSVP, ...] = ()
    lobby_id: str | None = None
    # Issue #67: tentative "Maybe" responses, tracked entirely separately
    # from ``rsvps``/``waitlist`` — they never occupy a seat or a waitlist
    # position.
    maybe: tuple[RSVP, ...] = ()
    # Issue #67: the durable public RSVP card location, so a restart or a
    # retried reconciliation edits the existing message instead of posting
    # a duplicate one. For a weekly series this same channel/message id is
    # carried forward to each new occurrence rather than reposted.
    delivery_channel_id: int | None = None
    delivery_message_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "starts_at", ensure_utc(self.starts_at))
        object.__setattr__(self, "recurrence", Recurrence(self.recurrence))
        object.__setattr__(self, "state", EventState(self.state))
        if self.capacity < 2 or self.capacity > 20:
            raise ScheduleError("capacity must be between 2 and 20")
        if not self.reminder_minutes or any(
            minutes < 5 or minutes > 10080 for minutes in self.reminder_minutes
        ):
            raise ScheduleError("reminders must be 5-10080 minutes before start")


def parse_local_start(value: str, timezone_name: str, *, now: datetime | None = None) -> datetime:
    """Parse a deliberately small natural-time vocabulary and normalize to UTC.

    Supported examples: ``2026-08-01 8:30 PM``, ``tomorrow 20:30``, and
    ``Friday 8 PM``. A named IANA timezone is mandatory so Discord always shows
    the organizer exactly what will be stored before confirmation.
    """
    timezone_name = timezone_name.strip()
    if "/" not in timezone_name:
        raise ScheduleError("use an IANA timezone such as America/New_York")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("use an IANA timezone such as America/New_York") from None
    current = ensure_utc(now) or datetime.now(timezone.utc)
    local_now = current.astimezone(zone)
    cleaned = " ".join(value.strip().split())
    absolute = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})[ T]+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        cleaned,
        re.IGNORECASE,
    )
    if absolute:
        day = datetime.strptime(absolute.group(1), "%Y-%m-%d").date()
        clock = _parse_clock(absolute.group(2))
    else:
        relative = re.fullmatch(
            r"(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(.+)",
            cleaned,
            re.IGNORECASE,
        )
        if not relative:
            raise ScheduleError(
                "time must look like `2026-08-01 8 PM`, `tomorrow 20:00`, or `Friday 8 PM`"
            )
        label = relative.group(1).lower()
        clock = _parse_clock(relative.group(2))
        if label == "today":
            offset = 0
        elif label == "tomorrow":
            offset = 1
        else:
            target = (
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
            ).index(label)
            offset = (target - local_now.weekday()) % 7 or 7
        day = (local_now + timedelta(days=offset)).date()
    local = _strict_local_time(day, clock, zone)
    normalized = local.astimezone(timezone.utc)
    if normalized <= current:
        raise ScheduleError("scheduled time must be in the future")
    return normalized


def _strict_local_time(day, clock, zone: ZoneInfo) -> datetime:
    """Attach a zone without silently changing or guessing the requested wall time."""
    naive = datetime.combine(day, clock)
    candidates = tuple(
        candidate
        for fold in (0, 1)
        if (
            candidate := naive.replace(tzinfo=zone, fold=fold)
        ).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive
    )
    if not candidates:
        raise ScheduleError(
            "that local time does not exist because of a daylight-saving clock change"
        )
    if len({candidate.utcoffset() for candidate in candidates}) > 1:
        raise ScheduleError(
            "that local time is ambiguous because of a daylight-saving clock change; "
            "choose a time outside the repeated hour"
        )
    return candidates[0]


def _parse_clock(value: str):
    compact = value.strip().upper()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(compact, fmt).time()
        except ValueError:
            pass
    raise ScheduleError("time of day must look like `8 PM` or `20:00`")


def _weekly_occurrence_at_or_after(
    starts_at: datetime, timezone_name: str, at: datetime
) -> datetime:
    """Return a weekly occurrence without applying seven-day arithmetic in UTC."""
    zone = ZoneInfo(timezone_name)
    local_start = ensure_utc(starts_at).astimezone(zone)
    local_at = ensure_utc(at).astimezone(zone)
    elapsed_weeks = max(0, (local_at.date() - local_start.date()).days // 7)
    occurrence = _strict_local_time(
        local_start.date() + timedelta(weeks=elapsed_weeks),
        local_start.time().replace(tzinfo=None),
        zone,
    ).astimezone(timezone.utc)
    if occurrence < at:
        occurrence = _strict_local_time(
            local_start.date() + timedelta(weeks=elapsed_weeks + 1),
            local_start.time().replace(tzinfo=None),
            zone,
        ).astimezone(timezone.utc)
    return occurrence


class ScheduleRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    @staticmethod
    def _ensure_schema(conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduled_nights (
              event_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
              organizer_id INTEGER NOT NULL, title TEXT NOT NULL,
              starts_at TEXT NOT NULL, timezone_name TEXT NOT NULL,
              recurrence TEXT NOT NULL, capacity INTEGER NOT NULL,
              role_slots TEXT NOT NULL, reminder_minutes TEXT NOT NULL,
              state TEXT NOT NULL, lobby_id TEXT
            );
            CREATE INDEX IF NOT EXISTS scheduled_nights_guild_start
              ON scheduled_nights(guild_id, starts_at);
            CREATE TABLE IF NOT EXISTS scheduled_rsvps (
              event_id TEXT NOT NULL REFERENCES scheduled_nights(event_id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL, position INTEGER NOT NULL,
              waitlisted INTEGER NOT NULL, primary_role TEXT, secondary_role TEXT,
              fill INTEGER NOT NULL, captain INTEGER NOT NULL, joined_at TEXT NOT NULL,
              PRIMARY KEY(event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS scheduled_reminders (
              event_id TEXT NOT NULL, occurrence_at TEXT NOT NULL,
              minutes_before INTEGER NOT NULL, sent_at TEXT,
              PRIMARY KEY(event_id, occurrence_at, minutes_before)
            );
            """
        )
        # Issue #67 additions.
        ScheduleRepository._add_columns(
            conn,
            "scheduled_nights",
            {
                "delivery_channel_id": "INTEGER",
                "delivery_message_id": "INTEGER",
                # Set only on the weekly successor row a rollover creates,
                # so the public-card reconciliation pass can find it without
                # guessing from title/guild/timing.
                "predecessor_event_id": "TEXT",
            },
        )
        ScheduleRepository._add_columns(
            conn,
            "scheduled_rsvps",
            {"response": "TEXT NOT NULL DEFAULT 'going'"},
        )

    @staticmethod
    def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def create(
        self, *, guild_id: int, organizer_id: int, title: str, starts_at: datetime,
        timezone_name: str, recurrence: Recurrence, capacity: int,
        role_slots: tuple[str, ...] = (), reminder_minutes: tuple[int, ...] = (60, 15),
        operation_id: str,
    ) -> ScheduledNight:
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"godforge:schedule:{guild_id}:{operation_id}"
        ).hex[:12]
        event = ScheduledNight(
            event_id, guild_id, organizer_id, title.strip(), starts_at,
            timezone_name, recurrence, capacity,
            tuple(role.strip().lower() for role in role_slots if role.strip()),
            tuple(sorted(set(reminder_minutes), reverse=True)),
        )
        with self._transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO scheduled_nights
                   (event_id,guild_id,organizer_id,title,starts_at,timezone_name,
                    recurrence,capacity,role_slots,reminder_minutes,state,lobby_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    event.event_id, event.guild_id, event.organizer_id, event.title,
                    event.starts_at.isoformat(), event.timezone_name, event.recurrence,
                    event.capacity, ",".join(event.role_slots),
                    ",".join(map(str, event.reminder_minutes)), event.state,
                ),
            )
        return self.get(event_id)

    def get(self, event_id: str) -> ScheduledNight | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_nights WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            members = conn.execute(
                "SELECT * FROM scheduled_rsvps WHERE event_id=? ORDER BY position",
                (event_id,),
            ).fetchall()
            return self._decode(row, members)

    def list_upcoming(self, guild_id: int, *, now: datetime | None = None) -> list[ScheduledNight]:
        at = (ensure_utc(now) or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            ids = conn.execute(
                """SELECT event_id FROM scheduled_nights
                   WHERE guild_id=? AND starts_at>=? AND state IN (?,?)
                   ORDER BY starts_at""",
                (guild_id, at, EventState.PENDING_CONFIRMATION, EventState.SCHEDULED),
            ).fetchall()
        return [event for row in ids if (event := self.get(row["event_id"]))]

    def confirm(self, event_id: str, organizer_id: int) -> ScheduledNight:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT organizer_id,state FROM scheduled_nights WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise ScheduleError("scheduled night not found")
            if row["organizer_id"] != organizer_id:
                raise ScheduleError("only the organizer can confirm this time")
            if row["state"] == EventState.PENDING_CONFIRMATION:
                conn.execute(
                    "UPDATE scheduled_nights SET state=? WHERE event_id=?",
                    (EventState.SCHEDULED, event_id),
                )
        return self.get(event_id)

    def rsvp(self, event_id: str, user_id: int, preferences: PlayerPreferences) -> ScheduledNight:
        """Reserve a Going seat (or promote an existing Maybe response to Going).

        A player who is already Going keeps their queue position, but their
        stored role preferences are refreshed — needed for the RSVP card's
        "Change Roles" escape hatch, which re-submits this same call with
        updated preferences for a player who never left Going. A player who
        was Maybe is moved into the Going roster/waitlist at the back of the
        line, same as a brand-new RSVP.
        """
        with self._transaction() as conn:
            event = self._required(conn, event_id)
            if event["state"] != EventState.SCHEDULED:
                raise ScheduleError("RSVPs require a confirmed scheduled night")
            prior = conn.execute(
                "SELECT response FROM scheduled_rsvps WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            ).fetchone()
            if prior is not None and prior["response"] == "going":
                conn.execute(
                    """UPDATE scheduled_rsvps SET primary_role=?,secondary_role=?,
                       fill=?,captain=? WHERE event_id=? AND user_id=?""",
                    (
                        preferences.primary_role, preferences.secondary_role,
                        int(preferences.fill), int(preferences.captain),
                        event_id, user_id,
                    ),
                )
            else:
                count = conn.execute(
                    "SELECT COUNT(*) count FROM scheduled_rsvps WHERE event_id=? AND response='going'",
                    (event_id,),
                ).fetchone()["count"]
                position, waitlisted = count + 1, int(count >= event["capacity"])
                if prior is None:
                    conn.execute(
                        """INSERT INTO scheduled_rsvps
                           (event_id,user_id,position,waitlisted,response,primary_role,
                            secondary_role,fill,captain,joined_at)
                           VALUES (?,?,?,?,'going',?,?,?,?,?)""",
                        (
                            event_id, user_id, position, waitlisted,
                            preferences.primary_role, preferences.secondary_role,
                            int(preferences.fill), int(preferences.captain),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE scheduled_rsvps SET response='going',position=?,waitlisted=?,
                           primary_role=?,secondary_role=?,fill=?,captain=?
                           WHERE event_id=? AND user_id=?""",
                        (
                            position, waitlisted,
                            preferences.primary_role, preferences.secondary_role,
                            int(preferences.fill), int(preferences.captain),
                            event_id, user_id,
                        ),
                    )
        return self.get(event_id)

    def rsvp_maybe(
        self, event_id: str, user_id: int, preferences: PlayerPreferences
    ) -> ScheduledNight:
        """Record a tentative response — never occupies a seat or waitlist slot.

        A player who was already Going has their seat released first (and
        anyone behind them promoted), exactly like ``cancel_rsvp``, since
        Maybe is not a stronger commitment than Going.
        """
        with self._transaction() as conn:
            event = self._required(conn, event_id)
            if event["state"] != EventState.SCHEDULED:
                raise ScheduleError("RSVPs require a confirmed scheduled night")
            prior = conn.execute(
                "SELECT response FROM scheduled_rsvps WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            ).fetchone()
            if prior is not None and prior["response"] == "going":
                self._remove_going_and_reindex(conn, event_id, user_id, event["capacity"])
            conn.execute(
                """INSERT INTO scheduled_rsvps
                   (event_id,user_id,position,waitlisted,response,primary_role,
                    secondary_role,fill,captain,joined_at)
                   VALUES (?,?,0,0,'maybe',?,?,?,?,?)
                   ON CONFLICT(event_id,user_id) DO UPDATE SET
                     response='maybe',position=0,waitlisted=0,
                     primary_role=excluded.primary_role,
                     secondary_role=excluded.secondary_role,
                     fill=excluded.fill,captain=excluded.captain""",
                (
                    event_id, user_id,
                    preferences.primary_role, preferences.secondary_role,
                    int(preferences.fill), int(preferences.captain),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return self.get(event_id)

    def cancel_rsvp(self, event_id: str, user_id: int) -> ScheduledNight:
        """Remove any Going or Maybe response — the "Can't Make It" action."""
        with self._transaction() as conn:
            event = self._required(conn, event_id)
            prior = conn.execute(
                "SELECT response FROM scheduled_rsvps WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            ).fetchone()
            if prior is None:
                return self.get(event_id)
            if prior["response"] == "going":
                self._remove_going_and_reindex(conn, event_id, user_id, event["capacity"])
            else:
                conn.execute(
                    "DELETE FROM scheduled_rsvps WHERE event_id=? AND user_id=?",
                    (event_id, user_id),
                )
        return self.get(event_id)

    @staticmethod
    def _remove_going_and_reindex(conn, event_id: str, user_id: int, capacity: int) -> None:
        conn.execute(
            "DELETE FROM scheduled_rsvps WHERE event_id=? AND user_id=? AND response='going'",
            (event_id, user_id),
        )
        rows = conn.execute(
            "SELECT user_id FROM scheduled_rsvps WHERE event_id=? AND response='going' "
            "ORDER BY position",
            (event_id,),
        ).fetchall()
        for index, row in enumerate(rows):
            conn.execute(
                """UPDATE scheduled_rsvps SET position=?,waitlisted=?
                   WHERE event_id=? AND user_id=?""",
                (index + 1, int(index >= capacity), event_id, row["user_id"]),
            )

    def mark_converted(self, event_id: str, lobby_id: str) -> ScheduledNight:
        """Convert this occurrence and, for a weekly series, schedule the next one.

        Issue #67: the public RSVP card's delivery reference transfers to the
        newly-created successor row rather than being reposted — this is what
        lets the periodic reconciliation edit the *same* Discord message
        forward to the next occurrence instead of accumulating a new card
        every week. A one-time night keeps its delivery ref on the converted
        row itself, since there's no successor to hand it to; the caller is
        responsible for one final edit showing the "session started" state.
        """
        with self._transaction() as conn:
            event = self._required(conn, event_id)
            if event["lobby_id"] and event["lobby_id"] != lobby_id:
                raise ScheduleError("scheduled night already converted to another lobby")
            if event["recurrence"] == Recurrence.WEEKLY:
                zone = ZoneInfo(event["timezone_name"])
                prior_local = datetime.fromisoformat(event["starts_at"]).astimezone(zone)
                next_local = _strict_local_time(
                    prior_local.date() + timedelta(weeks=1),
                    prior_local.time().replace(tzinfo=None),
                    zone,
                )
                next_start = next_local.astimezone(timezone.utc)
                next_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"godforge:schedule:{event_id}:{next_start.isoformat()}",
                ).hex[:12]
                conn.execute(
                    """INSERT OR IGNORE INTO scheduled_nights
                       (event_id,guild_id,organizer_id,title,starts_at,timezone_name,
                        recurrence,capacity,role_slots,reminder_minutes,state,lobby_id,
                        delivery_channel_id,delivery_message_id,predecessor_event_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
                    (
                        next_id, event["guild_id"], event["organizer_id"], event["title"],
                        next_start.isoformat(), event["timezone_name"], event["recurrence"],
                        event["capacity"], event["role_slots"], event["reminder_minutes"],
                        EventState.SCHEDULED,
                        event["delivery_channel_id"], event["delivery_message_id"], event_id,
                    ),
                )
                conn.execute(
                    "UPDATE scheduled_nights SET state=?,lobby_id=?,"
                    "delivery_channel_id=NULL,delivery_message_id=NULL WHERE event_id=?",
                    (EventState.CONVERTED, lobby_id, event_id),
                )
            else:
                conn.execute(
                    "UPDATE scheduled_nights SET state=?,lobby_id=? WHERE event_id=?",
                    (EventState.CONVERTED, lobby_id, event_id),
                )
        return self.get(event_id)

    def find_successor(self, event_id: str) -> ScheduledNight | None:
        """The weekly-rollover occurrence created when ``event_id`` converted, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM scheduled_nights WHERE predecessor_event_id=?",
                (event_id,),
            ).fetchone()
        return self.get(row["event_id"]) if row else None

    def set_delivery(
        self, event_id: str, channel_id: int | None, message_id: int | None
    ) -> ScheduledNight:
        """Persist (or clear) the durable public RSVP card location."""
        with self._transaction() as conn:
            self._required(conn, event_id)
            conn.execute(
                "UPDATE scheduled_nights SET delivery_channel_id=?,delivery_message_id=? "
                "WHERE event_id=?",
                (channel_id, message_id, event_id),
            )
        return self.get(event_id)

    def list_all_upcoming(self, *, now: datetime | None = None) -> list[ScheduledNight]:
        """Every guild's confirmed/pending scheduled nights, for cross-guild reconciliation."""
        at = (ensure_utc(now) or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            ids = conn.execute(
                """SELECT event_id FROM scheduled_nights
                   WHERE starts_at>=? AND state IN (?,?)
                   ORDER BY starts_at""",
                (at, EventState.PENDING_CONFIRMATION, EventState.SCHEDULED),
            ).fetchall()
        return [event for row in ids if (event := self.get(row["event_id"]))]

    def claim_due_reminders(
        self, *, now: datetime | None = None
    ) -> list[tuple[ScheduledNight, int, datetime]]:
        """Atomically claim reminder deliveries so retries and restarts do not duplicate them."""
        at = ensure_utc(now) or datetime.now(timezone.utc)
        due: list[tuple[ScheduledNight, int, datetime]] = []
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT event_id,starts_at,timezone_name,recurrence,reminder_minutes "
                "FROM scheduled_nights "
                "WHERE state=?",
                (EventState.SCHEDULED,),
            ).fetchall()
            for row in rows:
                occurrence = datetime.fromisoformat(row["starts_at"])
                if row["recurrence"] == Recurrence.WEEKLY and occurrence < at:
                    occurrence = _weekly_occurrence_at_or_after(
                        occurrence, row["timezone_name"], at
                    )
                for minutes in (int(v) for v in row["reminder_minutes"].split(",") if v):
                    delivery_at = occurrence - timedelta(minutes=minutes)
                    if not delivery_at <= at < occurrence:
                        continue
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO scheduled_reminders
                           (event_id,occurrence_at,minutes_before,sent_at)
                           VALUES (?,?,?,?)""",
                        (row["event_id"], occurrence.isoformat(), minutes, at.isoformat()),
                    )
                    if cursor.rowcount:
                        due.append((row["event_id"], minutes, occurrence))
        return [
            (event, minutes, occurrence)
            for event_id, minutes, occurrence in due
            if (event := self.get(event_id))
        ]

    @staticmethod
    def _required(conn, event_id):
        row = conn.execute(
            "SELECT * FROM scheduled_nights WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise ScheduleError("scheduled night not found")
        return row

    @staticmethod
    def _decode(row, members) -> ScheduledNight:
        decoded = [
            RSVP(
                member["user_id"],
                PlayerPreferences(
                    member["primary_role"], member["secondary_role"],
                    bool(member["fill"]), bool(member["captain"]),
                ),
                datetime.fromisoformat(member["joined_at"]),
            )
            for member in members
        ]
        active = tuple(
            item for item, member in zip(decoded, members)
            if member["response"] == "going" and not member["waitlisted"]
        )
        waitlist = tuple(
            item for item, member in zip(decoded, members)
            if member["response"] == "going" and member["waitlisted"]
        )
        maybe = tuple(
            item for item, member in zip(decoded, members) if member["response"] == "maybe"
        )
        return ScheduledNight(
            row["event_id"], row["guild_id"], row["organizer_id"], row["title"],
            datetime.fromisoformat(row["starts_at"]), row["timezone_name"],
            row["recurrence"], row["capacity"],
            tuple(filter(None, row["role_slots"].split(","))),
            tuple(int(v) for v in row["reminder_minutes"].split(",") if v),
            row["state"], active, waitlist, row["lobby_id"], maybe,
            row["delivery_channel_id"], row["delivery_message_id"],
        )


async def convert_to_lobby(event: ScheduledNight, schedules: ScheduleRepository, parties, queues):
    """Idempotently convert RSVPs into the canonical lobby and queue."""
    if event.state not in {EventState.SCHEDULED, EventState.CONVERTED}:
        raise ScheduleError("confirm the scheduled time before creating its lobby")
    lobby_id = event.lobby_id or f"scheduled-{event.event_id}"
    lobby = parties.create(
        guild_id=event.guild_id,
        organizer_id=event.organizer_id,
        capacity=event.capacity,
        expires_at=event.starts_at + timedelta(hours=4),
        lobby_id=lobby_id,
        operation_id=f"schedule:{event.event_id}:lobby",
        mode="custom",
        format="scheduled night",
        notes=f"{event.title} - {event.recurrence.value}",
    )
    queue = await queues.get(lobby_id)
    if queue is None:
        await queues.create(lobby_id, event.capacity)
    for rsvp in event.rsvps:
        participant = Participant(
            rsvp.user_id,
            primary_role=rsvp.preferences.primary_role,
            secondary_role=rsvp.preferences.secondary_role,
            fill=rsvp.preferences.fill,
            captain=rsvp.preferences.captain,
            joined_at=rsvp.joined_at,
        )
        lobby = parties.save_participant(
            event.guild_id, lobby_id, participant,
            operation_id=f"schedule:{event.event_id}:participant:{rsvp.user_id}",
        )
        await queues.join(lobby_id, rsvp.user_id, participant.preferences)
    for rsvp in event.waitlist:
        await queues.join(lobby_id, rsvp.user_id, rsvp.preferences.roles)
    queue = await queues.get(lobby_id)
    lobby = parties.get(event.guild_id, lobby_id)
    if queue and len(queue.active) == queue.capacity:
        if lobby.state is LobbyState.OPEN:
            lobby = parties.transition(
                event.guild_id,
                lobby_id,
                LobbyState.FULL,
                operation_id=f"schedule:{event.event_id}:full",
                actor_id=event.organizer_id,
                reason="scheduled RSVP roster reached capacity",
            )
        if queue.status is QueueStatus.OPEN:
            queue = await queues.start_ready_check(lobby_id)
        if lobby.state is LobbyState.FULL:
            lobby = parties.transition(
                event.guild_id,
                lobby_id,
                LobbyState.READY_CHECK,
                operation_id=f"schedule:{event.event_id}:ready-check",
                actor_id=event.organizer_id,
                reason="scheduled full roster ready check",
            )
    schedules.mark_converted(event.event_id, lobby_id)
    return parties.get(event.guild_id, lobby_id)


def calendar_ics(event: ScheduledNight) -> bytes:
    stamp = event.starts_at.strftime("%Y%m%dT%H%M%SZ")
    zone = ZoneInfo(event.timezone_name)
    local_start = event.starts_at.astimezone(zone)
    local_end = local_start + timedelta(hours=3)
    local_format = "%Y%m%dT%H%M%S"
    start = local_start.strftime(local_format)
    end = local_end.strftime(local_format)
    interval = "\r\nRRULE:FREQ=WEEKLY" if event.recurrence is Recurrence.WEEKLY else ""
    title = (
        event.title.replace("\r", " ").replace("\n", " ")
        .replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
    )
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//GodForge//SMITE Night//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event.event_id}@godforge\r\nDTSTAMP:{stamp}\r\n"
        f"DTSTART;TZID={event.timezone_name}:{start}\r\n"
        f"DTEND;TZID={event.timezone_name}:{end}\r\nSUMMARY:{title}{interval}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return body.encode("utf-8")
