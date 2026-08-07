"""Durable temporary-room orchestration behind a small Discord adapter."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol


class RoomState(StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    CLOSING = "closing"
    CLOSED = "closed"
    ORPHANED = "orphaned"


class RoomClosureOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_CONTEST = "no_contest"


class RoomPermissionError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class MatchRooms:
    lobby_id: str
    guild_id: int
    organizer_id: int
    participant_ids: tuple[int, ...]
    text_room_id: int | None = None
    team_voice_ids: tuple[int, ...] = ()
    state: RoomState = RoomState.OPEN
    empty_since: datetime | None = None
    archive_message_id: int | None = None
    updated_at: datetime = datetime.min.replace(tzinfo=UTC)

    @property
    def resource_ids(self) -> tuple[int, ...]:
        return tuple(
            resource_id
            for resource_id in (self.text_room_id, *self.team_voice_ids)
            if resource_id is not None
        )


@dataclass(frozen=True, slots=True)
class RoomClosureReport:
    report_id: str
    guild_id: int
    lobby_id: str
    organizer_id: int
    participant_ids: tuple[int, ...]
    outcome: RoomClosureOutcome
    close_reason: str
    closed_at: datetime
    created_at: datetime


class MatchRoomOperations(Protocol):
    guild_id: int

    async def resource_exists(self, resource_id: int) -> bool: ...

    async def create_private_rooms(
        self,
        lobby_id: str,
        organizer_id: int,
        participant_ids: tuple[int, ...],
        *,
        create_team_voice: bool,
    ) -> tuple[int, int | None, int | None]: ...

    async def set_locked(
        self,
        resource_ids: tuple[int, ...],
        participant_ids: tuple[int, ...],
        locked: bool,
    ) -> None: ...

    async def remove_player(self, resource_ids: tuple[int, ...], user_id: int) -> None: ...

    async def sync_participants(
        self,
        resource_ids: tuple[int, ...],
        participant_ids: tuple[int, ...],
        removed_ids: tuple[int, ...],
        locked: bool,
    ) -> None: ...

    async def transfer_organizer(
        self,
        resource_ids: tuple[int, ...],
        old_organizer_id: int,
        new_organizer_id: int,
    ) -> None: ...

    async def move_from_lobby_voice(
        self, user_id: int, lobby_voice_id: int | None, destination_id: int
    ) -> str | None: ...

    async def delete_resources(self, resource_ids: tuple[int, ...]) -> None: ...


class SQLiteMatchRoomRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS party_match_rooms (
                  lobby_id TEXT PRIMARY KEY,
                  guild_id INTEGER NOT NULL,
                  organizer_id INTEGER NOT NULL,
                  participant_ids_json TEXT NOT NULL,
                  text_room_id INTEGER,
                  team_voice_ids_json TEXT NOT NULL DEFAULT '[]',
                  state TEXT NOT NULL,
                  empty_since TEXT,
                  archive_message_id INTEGER,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS party_match_rooms_cleanup
                  ON party_match_rooms(state,empty_since);
                CREATE TABLE IF NOT EXISTS party_room_reports (
                  report_id TEXT PRIMARY KEY,
                  guild_id INTEGER NOT NULL,
                  lobby_id TEXT NOT NULL,
                  organizer_id INTEGER NOT NULL,
                  participant_ids_json TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  close_reason TEXT NOT NULL,
                  closed_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(guild_id, lobby_id)
                );
                CREATE INDEX IF NOT EXISTS party_room_reports_guild_closed
                  ON party_room_reports(guild_id, closed_at);
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self):
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def get(self, lobby_id: str) -> MatchRooms | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM party_match_rooms WHERE lobby_id=?", (lobby_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def active(self, guild_id: int | None = None) -> tuple[MatchRooms, ...]:
        query = "SELECT * FROM party_match_rooms WHERE state != ?"
        params: list[object] = [RoomState.CLOSED]
        if guild_id is not None:
            query += " AND guild_id=?"
            params.append(guild_id)
        query += " ORDER BY updated_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._decode(row) for row in rows)

    def save(self, rooms: MatchRooms) -> MatchRooms:
        now = _utc_now()
        rooms = replace(rooms, updated_at=now)
        with self._transaction() as conn:
            self._save(conn, rooms)
        return rooms

    def begin_closure(
        self,
        rooms: MatchRooms,
        *,
        outcome: RoomClosureOutcome,
        close_reason: str,
    ) -> tuple[MatchRooms, RoomClosureReport]:
        """Atomically persist closing intent and its immutable report."""
        now = _utc_now()
        closing = replace(
            rooms,
            state=RoomState.CLOSING,
            empty_since=rooms.empty_since or now,
            updated_at=now,
        )
        report_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"godforge:room-report:{rooms.guild_id}:{rooms.lobby_id}",
        ).hex
        with self._transaction() as conn:
            self._save(conn, closing)
            conn.execute(
                """INSERT OR IGNORE INTO party_room_reports
                   (report_id,guild_id,lobby_id,organizer_id,participant_ids_json,
                    outcome,close_reason,closed_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    report_id,
                    rooms.guild_id,
                    rooms.lobby_id,
                    rooms.organizer_id,
                    json.dumps(rooms.participant_ids),
                    outcome,
                    close_reason.strip() or "room closed",
                    _encode_time(now),
                    _encode_time(now),
                ),
            )
            row = conn.execute(
                "SELECT * FROM party_room_reports WHERE guild_id=? AND lobby_id=?",
                (rooms.guild_id, rooms.lobby_id),
            ).fetchone()
        return closing, self._decode_report(row)

    def get_report(
        self, guild_id: int, report_id_or_lobby_id: str
    ) -> RoomClosureReport | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM party_room_reports
                   WHERE guild_id=? AND (report_id=? OR lobby_id=?)""",
                (guild_id, report_id_or_lobby_id, report_id_or_lobby_id),
            ).fetchone()
        return self._decode_report(row) if row else None

    def reports(self, guild_id: int) -> tuple[RoomClosureReport, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM party_room_reports WHERE guild_id=?
                   ORDER BY closed_at, report_id""",
                (guild_id,),
            ).fetchall()
        return tuple(self._decode_report(row) for row in rows)

    @staticmethod
    def _save(conn, rooms: MatchRooms) -> None:
        conn.execute(
            """INSERT INTO party_match_rooms
               (lobby_id,guild_id,organizer_id,participant_ids_json,text_room_id,
                team_voice_ids_json,state,empty_since,archive_message_id,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(lobby_id) DO UPDATE SET
                guild_id=excluded.guild_id,organizer_id=excluded.organizer_id,
                participant_ids_json=excluded.participant_ids_json,
                text_room_id=excluded.text_room_id,
                team_voice_ids_json=excluded.team_voice_ids_json,state=excluded.state,
                empty_since=excluded.empty_since,
                archive_message_id=excluded.archive_message_id,
                updated_at=excluded.updated_at""",
            (
                rooms.lobby_id,
                rooms.guild_id,
                rooms.organizer_id,
                json.dumps(rooms.participant_ids),
                rooms.text_room_id,
                json.dumps(rooms.team_voice_ids),
                rooms.state,
                _encode_time(rooms.empty_since),
                rooms.archive_message_id,
                _encode_time(rooms.updated_at),
            ),
        )

    @staticmethod
    def _decode(row) -> MatchRooms:
        return MatchRooms(
            lobby_id=row["lobby_id"],
            guild_id=row["guild_id"],
            organizer_id=row["organizer_id"],
            participant_ids=tuple(json.loads(row["participant_ids_json"])),
            text_room_id=row["text_room_id"],
            team_voice_ids=tuple(json.loads(row["team_voice_ids_json"])),
            state=RoomState(row["state"]),
            empty_since=_decode_time(row["empty_since"]),
            archive_message_id=row["archive_message_id"],
            updated_at=_decode_time(row["updated_at"]),
        )

    @staticmethod
    def _decode_report(row) -> RoomClosureReport:
        return RoomClosureReport(
            report_id=row["report_id"],
            guild_id=row["guild_id"],
            lobby_id=row["lobby_id"],
            organizer_id=row["organizer_id"],
            participant_ids=tuple(json.loads(row["participant_ids_json"])),
            outcome=RoomClosureOutcome(row["outcome"]),
            close_reason=row["close_reason"],
            closed_at=_decode_time(row["closed_at"]),
            created_at=_decode_time(row["created_at"]),
        )


class MatchRoomService:
    def __init__(
        self,
        repository: SQLiteMatchRoomRepository,
        operations: MatchRoomOperations,
        *,
        empty_grace: timedelta = timedelta(minutes=10),
    ):
        if empty_grace < timedelta(0):
            raise ValueError("empty grace cannot be negative")
        self.repository = repository
        self.operations = operations
        self.empty_grace = empty_grace

    async def get(self, lobby_id: str) -> MatchRooms | None:
        return self.repository.get(lobby_id)

    async def get_report(
        self,
        report_id_or_lobby_id: str,
        *,
        guild_id: int,
        actor_id: int,
        is_staff: bool = False,
    ) -> RoomClosureReport:
        report = self.repository.get_report(guild_id, report_id_or_lobby_id)
        if report is None:
            raise LookupError("room report not found in this server")
        if actor_id != report.organizer_id and not is_staff:
            raise RoomPermissionError(
                "only the organizer or server staff can view this report"
            )
        return report

    async def provision(
        self,
        *,
        guild_id: int,
        lobby_id: str,
        organizer_id: int,
        participant_ids: tuple[int, ...],
        create_team_voice: bool,
    ) -> MatchRooms:
        existing = self.repository.get(lobby_id)
        if existing and existing.state is not RoomState.CLOSED:
            return await self.reconcile(lobby_id)
        return await self._create(
            guild_id, lobby_id, organizer_id, participant_ids, create_team_voice
        )

    async def reconcile(self, lobby_id: str) -> MatchRooms:
        rooms = self._required(lobby_id)
        if rooms.state is RoomState.CLOSED:
            return rooms
        if rooms.state is RoomState.CLOSING:
            return await self._report_and_delete(rooms, "resumed cleanup")
        existence = [
            await self.operations.resource_exists(resource_id)
            for resource_id in rooms.resource_ids
        ]
        if rooms.resource_ids and all(existence):
            if rooms.state is RoomState.ORPHANED:
                return self.repository.save(replace(rooms, state=RoomState.OPEN))
            return rooms
        # Replacement is created and persisted before the old partial set is
        # removed. A Discord create failure therefore leaves every surviving
        # room and its stored IDs available for the next reconciliation.
        replacement = await self._create(
            rooms.guild_id,
            rooms.lobby_id,
            rooms.organizer_id,
            rooms.participant_ids,
            bool(rooms.team_voice_ids),
        )
        if rooms.resource_ids:
            await self.operations.delete_resources(rooms.resource_ids)
        return replacement

    async def reconcile_all(self) -> tuple[MatchRooms, ...]:
        reconciled = []
        for rooms in self.repository.active(self.operations.guild_id):
            try:
                reconciled.append(await self.reconcile(rooms.lobby_id))
            except Exception:
                reconciled.append(
                    self.repository.save(replace(rooms, state=RoomState.ORPHANED))
                )
        return tuple(reconciled)

    async def lock(self, lobby_id: str, *, actor_id: int) -> MatchRooms:
        rooms = self._authorized(lobby_id, actor_id)
        await self.operations.set_locked(
            rooms.resource_ids, rooms.participant_ids, True
        )
        return self.repository.save(replace(rooms, state=RoomState.LOCKED))

    async def unlock(self, lobby_id: str, *, actor_id: int) -> MatchRooms:
        rooms = self._authorized(lobby_id, actor_id)
        await self.operations.set_locked(
            rooms.resource_ids, rooms.participant_ids, False
        )
        return self.repository.save(replace(rooms, state=RoomState.OPEN))

    async def remove_player(
        self, lobby_id: str, *, actor_id: int, user_id: int
    ) -> MatchRooms:
        rooms = self._authorized(lobby_id, actor_id)
        if user_id == rooms.organizer_id:
            raise ValueError("transfer ownership before removing the organizer")
        await self.operations.remove_player(rooms.resource_ids, user_id)
        return self.repository.save(
            replace(
                rooms,
                participant_ids=tuple(
                    participant_id
                    for participant_id in rooms.participant_ids
                    if participant_id != user_id
                ),
            )
        )

    async def reconcile_participants(
        self, lobby_id: str, participant_ids: tuple[int, ...]
    ) -> MatchRooms:
        """Apply a continuity roster to both Discord permissions and storage."""
        rooms = self._required(lobby_id)
        desired = tuple(dict.fromkeys(participant_ids))
        removed = tuple(set(rooms.participant_ids) - set(desired))
        await self.operations.sync_participants(
            rooms.resource_ids,
            desired,
            removed,
            rooms.state is RoomState.LOCKED,
        )
        return self.repository.save(replace(rooms, participant_ids=desired))

    async def transfer(
        self, lobby_id: str, *, actor_id: int, new_organizer_id: int
    ) -> MatchRooms:
        rooms = self._authorized(lobby_id, actor_id)
        if new_organizer_id not in rooms.participant_ids:
            raise ValueError("new organizer must be a lobby participant")
        await self.operations.transfer_organizer(
            rooms.resource_ids, rooms.organizer_id, new_organizer_id
        )
        return self.repository.save(
            replace(rooms, organizer_id=new_organizer_id)
        )

    async def transfer_transactionally(
        self,
        lobby_id: str,
        *,
        actor_id: int,
        new_organizer_id: int,
        commit: Callable[[], object],
        compensate: Callable[[], object],
    ) -> MatchRooms:
        """Coordinate room ownership with the authoritative party repository.

        The authoritative commit happens before Discord mutation. If Discord
        rejects the handoff, the supplied compensation restores party
        ownership while the room record remains unchanged.
        """
        rooms = self._authorized(lobby_id, actor_id)
        if new_organizer_id not in rooms.participant_ids:
            raise ValueError("new organizer must be a lobby participant")
        commit()
        try:
            await self.operations.transfer_organizer(
                rooms.resource_ids, rooms.organizer_id, new_organizer_id
            )
            return self.repository.save(
                replace(rooms, organizer_id=new_organizer_id)
            )
        except Exception:
            compensate()
            raise

    async def move_players(
        self,
        lobby_id: str,
        *,
        actor_id: int,
        lobby_voice_id: int | None,
        team_assignments: dict[int, int],
    ) -> dict[int, str]:
        rooms = self._authorized(lobby_id, actor_id)
        failures: dict[int, str] = {}
        for user_id, team_number in team_assignments.items():
            if user_id not in rooms.participant_ids:
                failures[user_id] = "Player is not in this lobby."
                continue
            if not 1 <= team_number <= len(rooms.team_voice_ids):
                failures[user_id] = "The assigned team voice room does not exist."
                continue
            error = await self.operations.move_from_lobby_voice(
                user_id, lobby_voice_id, rooms.team_voice_ids[team_number - 1]
            )
            if error:
                failures[user_id] = error
        return failures

    async def mark_empty(
        self, lobby_id: str, *, at: datetime | None = None
    ) -> MatchRooms:
        rooms = self._required(lobby_id)
        if rooms.state is RoomState.CLOSING and rooms.empty_since is not None:
            return rooms
        return self.repository.save(
            replace(rooms, state=RoomState.CLOSING, empty_since=_utc(at))
        )

    async def mark_occupied(self, lobby_id: str) -> MatchRooms:
        rooms = self._required(lobby_id)
        return self.repository.save(
            replace(rooms, state=RoomState.OPEN, empty_since=None)
        )

    async def close(
        self,
        lobby_id: str,
        *,
        actor_id: int | None = None,
        reason: str = "closed",
        outcome: RoomClosureOutcome | str | None = None,
    ) -> MatchRooms:
        rooms = self._required(lobby_id)
        if actor_id is not None and actor_id != rooms.organizer_id:
            raise RoomPermissionError("only the lobby organizer can control rooms")
        return await self._report_and_delete(rooms, reason, outcome=outcome)

    async def cleanup_due(
        self, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        current = _utc(now)
        cleaned = []
        for rooms in self.repository.active(self.operations.guild_id):
            if (
                rooms.state is RoomState.CLOSING
                and rooms.empty_since is not None
                and current >= rooms.empty_since + self.empty_grace
            ):
                await self._report_and_delete(rooms, "empty room grace elapsed")
                cleaned.append(rooms.lobby_id)
        return tuple(cleaned)

    async def _create(
        self,
        guild_id: int,
        lobby_id: str,
        organizer_id: int,
        participant_ids: tuple[int, ...],
        create_team_voice: bool,
    ) -> MatchRooms:
        text_id, team_one_id, team_two_id = (
            await self.operations.create_private_rooms(
                lobby_id,
                organizer_id,
                participant_ids,
                create_team_voice=create_team_voice,
            )
        )
        voices = tuple(
            resource_id
            for resource_id in (team_one_id, team_two_id)
            if resource_id is not None
        )
        return self.repository.save(
            MatchRooms(
                lobby_id=lobby_id,
                guild_id=guild_id,
                organizer_id=organizer_id,
                participant_ids=tuple(dict.fromkeys(participant_ids)),
                text_room_id=text_id,
                team_voice_ids=voices,
                updated_at=_utc_now(),
            )
        )

    async def _report_and_delete(
        self,
        rooms: MatchRooms,
        reason: str,
        *,
        outcome: RoomClosureOutcome | str | None = None,
    ) -> MatchRooms:
        rooms, _report = self.repository.begin_closure(
            rooms,
            outcome=_closure_outcome(outcome, reason),
            close_reason=reason,
        )
        await self.operations.delete_resources(rooms.resource_ids)
        return self.repository.save(
            replace(
                rooms,
                state=RoomState.CLOSED,
                empty_since=None,
            )
        )

    def _required(self, lobby_id: str) -> MatchRooms:
        rooms = self.repository.get(lobby_id)
        if rooms is None:
            raise LookupError("match rooms do not exist")
        return rooms

    def _authorized(self, lobby_id: str, actor_id: int) -> MatchRooms:
        rooms = self._required(lobby_id)
        if actor_id != rooms.organizer_id:
            raise RoomPermissionError("only the lobby organizer can control rooms")
        if rooms.state is RoomState.CLOSED:
            raise ValueError("match rooms are closed")
        return rooms


def _utc(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _closure_outcome(
    outcome: RoomClosureOutcome | str | None, reason: str
) -> RoomClosureOutcome:
    if outcome is not None:
        normalized = str(outcome).strip().lower().replace(" ", "_")
        if normalized == "expired":
            normalized = RoomClosureOutcome.CANCELLED
        return RoomClosureOutcome(normalized)
    lowered = reason.lower()
    if "complete" in lowered:
        return RoomClosureOutcome.COMPLETED
    if "cancel" in lowered or "organizer closed" in lowered or "expired" in lowered:
        return RoomClosureOutcome.CANCELLED
    return RoomClosureOutcome.NO_CONTEST


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _encode_time(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


def _decode_time(value: str | None) -> datetime | None:
    return _utc(datetime.fromisoformat(value)) if value else None
