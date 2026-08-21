from datetime import datetime, timedelta, timezone

import pytest

from utils.party import DiscordDelivery, LobbyState, Participant
from utils.party_store import OperationConflictError, SQLitePartyRepository


def repository(tmp_path):
    return SQLitePartyRepository(tmp_path / "parties.sqlite3")


def test_persists_complete_lobby_and_recovers_after_restart(tmp_path):
    repo = repository(tmp_path)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    lobby = repo.create(
        guild_id=42, organizer_id=7, capacity=5, expires_at=expires,
        lobby_id="stable-id", operation_id="discord:create:1",
        delivery=DiscordDelivery(panel_channel_id=11, panel_message_id=12),
    )
    repo.save_participant(
        42, lobby.lobby_id,
        Participant(99, ("Jungle", "Mid"), ready=True),
        operation_id="discord:join:1",
    )
    repo.transition(42, lobby.lobby_id, LobbyState.FULL, operation_id="discord:full:1")

    recovered = SQLitePartyRepository(repo.path).recover_active(42)

    assert len(recovered) == 1
    restored = recovered[0].lobby
    assert restored.lobby_id == "stable-id"
    assert restored.delivery.panel_message_id == 12
    assert restored.participant(99).preferences == ("jungle", "mid")
    assert restored.participant(99).ready is True
    assert restored.expires_at == expires


def test_guild_scope_prevents_cross_guild_access(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="same",
        operation_id="create",
    )
    assert repo.get(2, "same") is None
    assert repo.recover_active(2) == []


def test_retried_transition_is_idempotent_and_audited_once(tmp_path):
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )
    first = repo.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="interaction-1")
    retried = repo.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="interaction-1")

    assert retried.version == first.version
    assert [event.event_type for event in repo.audit_events(1, "lobby")] == [
        "created",
        "state_transition",
    ]


def test_retried_create_without_caller_supplied_id_is_idempotent(tmp_path):
    repo = repository(tmp_path)
    first = repo.create(
        guild_id=1, organizer_id=2, capacity=5, operation_id="interaction-create",
    )
    retried = repo.create(
        guild_id=1, organizer_id=2, capacity=5, operation_id="interaction-create",
    )
    assert retried.lobby_id == first.lobby_id
    assert len(repo.audit_events(1, first.lobby_id)) == 1


def test_operation_id_cannot_be_reused_for_another_command(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )
    repo.transition(1, "lobby", LobbyState.FULL, operation_id="interaction")
    with pytest.raises(OperationConflictError):
        repo.transition(1, "lobby", LobbyState.OPEN, operation_id="interaction")


def test_same_state_transition_consumes_operation_id(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )

    unchanged = repo.transition(
        1, "lobby", LobbyState.OPEN, operation_id="same-state",
    )

    assert unchanged.state == LobbyState.OPEN
    assert repo.audit_events(1, "lobby")[-1].event_type == "state_transition_noop"
    with pytest.raises(OperationConflictError):
        repo.save_participant(
            1, "lobby", Participant(3), operation_id="same-state",
        )


def test_delivery_references_can_be_reconciled_without_changing_identity(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="domain-id",
        operation_id="create",
    )
    changed = repo.set_delivery(
        1, "domain-id",
        DiscordDelivery(
            panel_channel_id=20, panel_message_id=21, voice_channel_id=22,
            team_channel_ids=(23, 24),
        ),
        operation_id="reconcile",
    )
    assert changed.lobby_id == "domain-id"
    assert changed.delivery.team_channel_ids == (23, 24)


def test_organizer_transfer_is_scoped_durable_and_audited(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )
    repo.save_participant(1, "lobby", Participant(2), operation_id="join-owner")
    repo.save_participant(1, "lobby", Participant(3), operation_id="join-next")

    changed = repo.transfer_organizer(
        1, "lobby", 3, operation_id="transfer", actor_id=2
    )

    assert changed.organizer_id == 3
    assert SQLitePartyRepository(repo.path).get(1, "lobby").organizer_id == 3
    assert repo.audit_events(1, "lobby")[-1].event_type == "organizer_transferred"
    with pytest.raises(PermissionError):
        repo.transfer_organizer(
            1, "lobby", 2, operation_id="old-owner", actor_id=2
        )


def test_terminal_lobbies_are_not_returned_for_recovery(tmp_path):
    repo = repository(tmp_path)
    for index, terminal in enumerate((LobbyState.CANCELLED, LobbyState.EXPIRED)):
        lobby_id = f"lobby-{index}"
        repo.create(
            guild_id=1, organizer_id=2, capacity=5, lobby_id=lobby_id,
            operation_id=f"create-{index}",
        )
        repo.transition(
            1, lobby_id, terminal, operation_id=f"terminal-{index}",
            reason="test cleanup",
        )
    assert repo.recover_active(1) == []
    assert repo.audit_events(1, "lobby-0")[-1].metadata == {"reason": "test cleanup"}


def test_recovery_does_not_hard_expire_a_lobby_awaiting_its_inactivity_prompt(tmp_path):
    # Issue #66: an elapsed recruiting clock no longer expires the lobby
    # directly — it needs a posted organizer confirmation prompt and an
    # unanswered grace period first, which requires Discord access this
    # store layer deliberately doesn't have. recover_active() must still
    # return it as active.
    repo = repository(tmp_path)
    repo.create(
        guild_id=1,
        organizer_id=2,
        capacity=5,
        lobby_id="elapsed",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )

    active = repo.recover_active(1)

    assert len(active) == 1
    assert active[0].lobby.state == LobbyState.OPEN
    due = repo.due_for_inactivity_prompt(1)
    assert [lobby.lobby_id for lobby in due] == ["elapsed"]


def test_inactivity_prompt_sent_advances_the_grace_deadline(tmp_path):
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="elapsed",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )

    updated = repo.record_inactivity_prompt_sent(
        1, "elapsed", 600, 601, operation_id="prompt-1",
        expected_expires_at=lobby.expires_at,
    )

    assert updated.delivery.inactivity_prompt_channel_id == 600
    assert updated.delivery.inactivity_prompt_message_id == 601
    assert updated.expires_at > lobby.expires_at
    # Now that a prompt exists, the lobby is no longer due for a second one.
    assert repo.due_for_inactivity_prompt(1) == []


def test_unanswered_grace_period_auto_closes_the_lobby(tmp_path):
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="elapsed",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )
    repo.record_inactivity_prompt_sent(
        1, "elapsed", 600, 601, operation_id="prompt-1", grace_minutes=-1,
        expected_expires_at=lobby.expires_at,
    )

    expired = repo.expire_due_recruitment(1)

    assert [lobby.lobby_id for lobby in expired] == ["elapsed"]
    closed = repo.get(1, "elapsed")
    assert closed.state == LobbyState.EXPIRED
    event = repo.audit_events(1, "elapsed")[-1]
    assert event.event_type == "expired"
    assert event.metadata == {"reason": "expires_at elapsed"}


def test_recording_a_prompt_is_rejected_if_the_clock_moved_underneath_it(tmp_path):
    # Compare-and-set guard: if meaningful activity (or a state change)
    # touched the lobby while the caller's channel.send() was in flight,
    # the now-unwanted prompt must not be recorded — the caller is
    # responsible for deleting the message it already posted.
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="elapsed",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )
    stale_expected = lobby.expires_at
    repo.touch_recruiting_activity(1, "elapsed", operation_id="touch-1")

    result = repo.record_inactivity_prompt_sent(
        1, "elapsed", 600, 601, operation_id="prompt-1",
        expected_expires_at=stale_expected,
    )

    assert result is None
    unchanged = repo.get(1, "elapsed")
    assert unchanged.delivery.inactivity_prompt_message_id is None


def test_ready_check_lobbies_are_not_covered_by_recruiting_expiry(tmp_path):
    # Issue #66 scope guardrail: READY_CHECK has its own fully separate
    # ready-deadline timeout (PartyQueueService) and must never be swept by
    # the recruiting-inactivity mechanism, even if a stale expires_at from
    # its OPEN/FULL days is still sitting on the row.
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="in-ready-check",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )
    repo.save_participant(1, lobby.lobby_id, Participant(2), operation_id="j1")
    repo.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    repo.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    assert repo.due_for_inactivity_prompt(1) == []
    assert repo.expire_due_recruitment(1) == []
    assert repo.get(1, lobby.lobby_id).state == LobbyState.READY_CHECK


def test_recovery_does_not_expire_active_game(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1,
        organizer_id=2,
        capacity=5,
        lobby_id="playing",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="create",
    )
    repo.transition(1, "playing", LobbyState.FULL, operation_id="full")
    repo.transition(1, "playing", LobbyState.READY_CHECK, operation_id="ready")
    repo.transition(1, "playing", LobbyState.FORMING, operation_id="forming")
    repo.transition(1, "playing", LobbyState.ACTIVE, operation_id="active")

    recovered = repo.recover_active(1)

    assert [record.lobby.lobby_id for record in recovered] == ["playing"]


def test_full_lobby_rejects_additional_participant(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=2, lobby_id="lobby",
        operation_id="create",
    )
    repo.save_participant(1, "lobby", Participant(2), operation_id="join-1")
    repo.save_participant(1, "lobby", Participant(3), operation_id="join-2")
    with pytest.raises(ValueError, match="full"):
        repo.save_participant(1, "lobby", Participant(4), operation_id="join-3")


def test_player_preferences_are_durable_and_guild_scoped(tmp_path):
    repo = repository(tmp_path)

    saved = repo.set_player_preferences(
        1,
        99,
        ["Jungle", "mid", "jungle", ""],
    )

    assert saved == ("jungle", "mid")
    restarted = SQLitePartyRepository(repo.path)
    assert restarted.get_player_preferences(1, 99) == ("jungle", "mid")
    assert restarted.get_player_preferences(2, 99) == ()
