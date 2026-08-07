from datetime import UTC, datetime, timedelta

import pytest

from utils.match_rooms import (
    MatchRoomService,
    MatchRooms,
    RoomClosureOutcome,
    RoomPermissionError,
    RoomState,
    SQLiteMatchRoomRepository,
)


class FakeRooms:
    def __init__(self):
        self.guild_id = 1
        self.resources = {}
        self.created = 0
        self.archives = []
        self.moved = []
        self.fail_moves = set()
        self.fail_create = False
        self.fail_transfer = False
        self.fail_delete = False

    async def resource_exists(self, resource_id):
        return resource_id in self.resources

    async def create_private_rooms(self, lobby_id, organizer_id, participant_ids, *, create_team_voice):
        if self.fail_create:
            raise RuntimeError("Discord creation failed")
        self.created += 1
        base = self.created * 100
        ids = (
            base + 1,
            base + 2 if create_team_voice else None,
            base + 3 if create_team_voice else None,
        )
        self.resources[ids[0]] = "text"
        if ids[1]:
            self.resources[ids[1]] = "voice"
        if ids[2]:
            self.resources[ids[2]] = "voice"
        return ids

    async def set_locked(self, resource_ids, participant_ids, locked):
        return None

    async def remove_player(self, resource_ids, user_id):
        return None

    async def transfer_organizer(self, resource_ids, old_organizer_id, new_organizer_id):
        if self.fail_transfer:
            raise RuntimeError("Discord transfer failed")
        return None

    async def move_from_lobby_voice(self, user_id, lobby_voice_id, destination_id):
        if user_id in self.fail_moves:
            return "GodForge cannot move this player; check Move Members permission."
        self.moved.append((user_id, lobby_voice_id, destination_id))
        return None

    async def archive_summary(self, summary):
        self.archives.append(summary)
        return 999

    async def delete_resources(self, resource_ids):
        if self.fail_delete:
            raise RuntimeError("Discord deletion failed")
        for resource_id in resource_ids:
            self.resources.pop(resource_id, None)


@pytest.mark.asyncio
async def test_provision_is_idempotent_and_survives_restart(tmp_path):
    path = tmp_path / "party.db"
    ops = FakeRooms()
    service = MatchRoomService(SQLiteMatchRoomRepository(path), ops)

    first = await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )
    restored = await MatchRoomService(
        SQLiteMatchRoomRepository(path), ops
    ).reconcile("lobby")

    assert restored == first
    assert ops.created == 1
    assert restored.state is RoomState.OPEN


@pytest.mark.asyncio
async def test_missing_resources_are_recreated_without_duplication(tmp_path):
    ops = FakeRooms()
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), ops
    )
    original = await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=False,
    )
    ops.resources.pop(original.text_room_id)

    repaired = await service.reconcile("lobby")

    assert ops.created == 2
    assert repaired.text_room_id != original.text_room_id


@pytest.mark.asyncio
async def test_failed_replacement_preserves_surviving_resources_and_stored_ids(tmp_path):
    ops = FakeRooms()
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), ops
    )
    original = await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )
    ops.resources.pop(original.team_voice_ids[-1])
    surviving = set(original.resource_ids).intersection(ops.resources)
    ops.fail_create = True

    with pytest.raises(RuntimeError, match="creation failed"):
        await service.reconcile("lobby")

    assert surviving <= set(ops.resources)
    assert await service.get("lobby") == original


@pytest.mark.asyncio
async def test_organizer_controls_are_lobby_scoped(tmp_path):
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), FakeRooms()
    )
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11, 12),
        create_team_voice=True,
    )

    with pytest.raises(RoomPermissionError):
        await service.lock("lobby", actor_id=11)
    locked = await service.lock("lobby", actor_id=10)
    unlocked = await service.unlock("lobby", actor_id=10)
    transferred = await service.transfer("lobby", actor_id=10, new_organizer_id=11)
    removed = await service.remove_player("lobby", actor_id=11, user_id=12)

    assert locked.state is RoomState.LOCKED
    assert unlocked.state is RoomState.OPEN
    assert transferred.organizer_id == 11
    assert removed.participant_ids == (10, 11)


@pytest.mark.asyncio
async def test_transactional_transfer_does_not_split_authority_on_party_failure(tmp_path):
    ops = FakeRooms()
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), ops
    )
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )

    def fail_party_transfer():
        raise RuntimeError("party transfer failed")

    with pytest.raises(RuntimeError, match="party transfer failed"):
        await service.transfer_transactionally(
            "lobby",
            actor_id=10,
            new_organizer_id=11,
            commit=fail_party_transfer,
            compensate=lambda: None,
        )

    assert (await service.get("lobby")).organizer_id == 10


@pytest.mark.asyncio
async def test_transactional_transfer_compensates_party_when_discord_fails(tmp_path):
    ops = FakeRooms()
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), ops
    )
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )
    operations = []
    ops.fail_transfer = True

    with pytest.raises(RuntimeError, match="Discord transfer failed"):
        await service.transfer_transactionally(
            "lobby",
            actor_id=10,
            new_organizer_id=11,
            commit=lambda: operations.append("commit"),
            compensate=lambda: operations.append("compensate"),
        )

    assert operations == ["commit", "compensate"]
    assert (await service.get("lobby")).organizer_id == 10


@pytest.mark.asyncio
async def test_voice_moves_report_per_player_failures(tmp_path):
    ops = FakeRooms()
    ops.fail_moves.add(12)
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"), ops
    )
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11, 12),
        create_team_voice=True,
    )

    failures = await service.move_players(
        "lobby", actor_id=10, lobby_voice_id=50, team_assignments={11: 1, 12: 2}
    )

    assert failures == {12: "GodForge cannot move this player; check Move Members permission."}
    assert ops.moved == [(11, 50, 102)]


@pytest.mark.asyncio
async def test_cleanup_saves_report_before_deleting_after_grace(tmp_path):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    ops = FakeRooms()
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"),
        ops,
        empty_grace=timedelta(minutes=5),
    )
    room = await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )

    await service.mark_empty("lobby", at=now)
    assert await service.cleanup_due(now=now + timedelta(minutes=4)) == ()
    cleaned = await service.cleanup_due(now=now + timedelta(minutes=5))

    assert cleaned == ("lobby",)
    report = service.repository.get_report(1, "lobby")
    assert report is not None
    assert report.lobby_id == "lobby"
    assert report.organizer_id == 10
    assert report.participant_ids == (10, 11)
    assert report.outcome is RoomClosureOutcome.NO_CONTEST
    assert report.close_reason == "empty room grace elapsed"
    assert ops.archives == []
    assert all(resource_id not in ops.resources for resource_id in room.resource_ids)
    assert (await service.get("lobby")).state is RoomState.CLOSED


@pytest.mark.asyncio
async def test_repeated_empty_events_preserve_original_grace_deadline(tmp_path):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    service = MatchRoomService(
        SQLiteMatchRoomRepository(tmp_path / "party.db"),
        FakeRooms(),
        empty_grace=timedelta(minutes=5),
    )
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=True,
    )

    first = await service.mark_empty("lobby", at=now)
    repeated = await service.mark_empty(
        "lobby", at=now + timedelta(minutes=4)
    )

    assert repeated.empty_since == first.empty_since == now
    assert await service.cleanup_due(now=now + timedelta(minutes=5)) == ("lobby",)


@pytest.mark.asyncio
async def test_cleanup_due_only_processes_adapter_guild(tmp_path):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    repository = SQLiteMatchRoomRepository(tmp_path / "party.db")
    guild_one_ops = FakeRooms()
    guild_two_ops = FakeRooms()
    guild_two_ops.guild_id = 2
    guild_one = MatchRoomService(
        repository, guild_one_ops, empty_grace=timedelta(0)
    )
    guild_two = MatchRoomService(
        repository, guild_two_ops, empty_grace=timedelta(0)
    )
    await guild_one.provision(
        guild_id=1, lobby_id="one", organizer_id=10,
        participant_ids=(10, 11), create_team_voice=True,
    )
    await guild_two.provision(
        guild_id=2, lobby_id="two", organizer_id=20,
        participant_ids=(20, 21), create_team_voice=True,
    )
    await guild_one.mark_empty("one", at=now)
    await guild_two.mark_empty("two", at=now)

    assert await guild_one.cleanup_due(now=now) == ("one",)
    assert (await guild_two.get("two")).state is RoomState.CLOSING
    assert repository.get_report(1, "one") is not None
    assert repository.get_report(2, "one") is None
    assert guild_one_ops.archives == []


@pytest.mark.asyncio
async def test_restart_resumes_persisted_closing_intent_without_recreating(tmp_path):
    path = tmp_path / "party.db"
    ops = FakeRooms()
    service = MatchRoomService(SQLiteMatchRoomRepository(path), ops)
    await service.provision(
        guild_id=1, lobby_id="lobby", organizer_id=10,
        participant_ids=(10, 11), create_team_voice=True,
    )
    ops.fail_delete = True

    with pytest.raises(RuntimeError, match="deletion failed"):
        await service.close("lobby", actor_id=10)

    interrupted = await service.get("lobby")
    assert interrupted.state is RoomState.CLOSING
    report = service.repository.get_report(1, "lobby")
    assert report is not None
    assert interrupted.archive_message_id is None
    assert ops.archives == []

    ops.fail_delete = False
    created_before = ops.created
    restored = await MatchRoomService(
        SQLiteMatchRoomRepository(path), ops
    ).reconcile("lobby")

    assert restored.state is RoomState.CLOSED
    assert ops.created == created_before
    assert restored.archive_message_id is None
    assert service.repository.get_report(1, "lobby") == report
    assert len(service.repository.reports(1)) == 1


@pytest.mark.asyncio
async def test_report_lookup_is_guild_scoped_and_authorized(tmp_path):
    repository = SQLiteMatchRoomRepository(tmp_path / "party.db")
    service = MatchRoomService(repository, FakeRooms())
    await service.provision(
        guild_id=1,
        lobby_id="lobby",
        organizer_id=10,
        participant_ids=(10, 11),
        create_team_voice=False,
    )
    await service.close("lobby", actor_id=10, outcome=RoomClosureOutcome.CANCELLED)
    saved = repository.get_report(1, "lobby")

    assert saved == await service.get_report(
        saved.report_id, guild_id=1, actor_id=10
    )
    assert saved == await service.get_report(
        "lobby", guild_id=1, actor_id=99, is_staff=True
    )
    with pytest.raises(RoomPermissionError):
        await service.get_report("lobby", guild_id=1, actor_id=11)
    with pytest.raises(LookupError):
        await service.get_report("lobby", guild_id=2, actor_id=10, is_staff=True)


def test_existing_database_migrates_additively_and_keeps_archive_id(tmp_path):
    path = tmp_path / "party.db"
    repository = SQLiteMatchRoomRepository(path)
    repository.save(
        MatchRooms(
            lobby_id="legacy",
            guild_id=1,
            organizer_id=10,
            participant_ids=(10, 11),
            archive_message_id=123,
        )
    )

    reopened = SQLiteMatchRoomRepository(path)

    assert reopened.get("legacy").archive_message_id == 123
    assert reopened.get_report(1, "legacy") is None
