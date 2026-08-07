from dataclasses import replace

import pytest

from utils.party import LobbyState, Participant, PartyLobby
from utils.party_draft import (
    DraftTeam,
    PartyDraftError,
    PartyDraftLaunchRepository,
    form_teams,
)
from utils.draft import DraftManager
from utils.match_history import MatchHistoryRepository


def _ready_lobby():
    players = tuple(
        Participant(
            user_id,
            primary_role=role,
            captain=user_id in {1, 2},
        )
        for user_id, role in enumerate(
            ("solo", "jungle", "mid", "support", "adc") * 2, start=1
        )
    )
    return PartyLobby(
        "lobby-1",
        guild_id=10,
        organizer_id=99,
        capacity=10,
        state=LobbyState.FORMING,
        participants=players,
        mode="conquest",
        region="na east",
        format="pug",
        notes="fearless",
    )


def test_form_teams_spreads_volunteer_captains_deterministically():
    blue, red = form_teams(_ready_lobby())

    assert blue.captain_id == 1
    assert red.captain_id == 2
    assert set(blue.participant_ids + red.participant_ids) == set(range(1, 11))


def test_form_teams_supports_six_player_party_snapshot():
    roles = ("solo", "mid", "adc") * 2
    lobby = replace(
        _ready_lobby(),
        capacity=6,
        participants=tuple(
            Participant(
                user_id,
                primary_role=role,
                captain=user_id in {1, 4},
            )
            for user_id, role in enumerate(roles, start=1)
        ),
    )

    blue, red = form_teams(lobby)

    assert len(blue.participant_ids) == len(red.participant_ids) == 3
    assert set(blue.participant_ids + red.participant_ids) == set(range(1, 7))


def test_form_teams_requires_ready_lobby():
    with pytest.raises(PartyDraftError, match="ready check"):
        form_teams(replace(_ready_lobby(), state=LobbyState.OPEN))


def test_launch_is_idempotent_and_retains_party_context(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    ids = iter(("GF-0042", "GF-0043"))

    first, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: next(ids),
    )
    retry, retry_should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: next(ids),
    )

    assert should_start is True
    assert retry_should_start is False
    assert retry.match_id == first.match_id == "GF-0042"
    assert first.snapshot["lobby_id"] == "lobby-1"
    assert first.snapshot["rules"]["mode"] == "conquest"
    assert first.snapshot["participants"][0]["assigned_roles"] == ["solo"]
    assert first.snapshot["formation"]["mode"] == "role_fit"
    assert first.snapshot["formation"]["first_choices"] == 10
    assert len(first.snapshot["formation"]["blue"]) == 5


def test_fixed_premade_teams_preserve_interleaved_roster_partition(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    blue_ids = (1, 3, 5, 7, 9)
    red_ids = (2, 4, 6, 8, 10)
    roles = ("solo", "jungle", "mid", "support", "adc")
    launch, _ = repo.begin(
        _ready_lobby(), operation_id="scrim-launch", channel_id=500,
        match_id_factory=lambda: "GF-SCRIM",
        fixed_teams=(
            DraftTeam(1, blue_ids, tuple(zip(blue_ids, roles, strict=True))),
            DraftTeam(2, red_ids, tuple(zip(red_ids, roles, strict=True))),
        ),
    )
    assert launch.blue.participant_ids == blue_ids
    assert launch.red.participant_ids == red_ids
    assert launch.snapshot["formation"]["mode"] == "premade_scrim"
    assert [item["user_id"] for item in launch.snapshot["formation"]["blue"]] == list(
        blue_ids
    )


def test_launch_snapshot_retains_godforge_owned_balance_inputs(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    organizer_inputs = {
        user_id: {
            "skill_band": "competitive" if user_id <= 5 else "beginner",
            "experience": user_id,
            "recent_adjustment": -1,
        }
        for user_id in range(1, 11)
    }

    launch, _ = repo.begin(
        _ready_lobby(),
        operation_id="balanced-interaction",
        channel_id=500,
        match_id_factory=lambda: "GF-0044",
        formation_mode="balanced",
        organizer_inputs=organizer_inputs,
    )

    assert launch.snapshot["formation"]["mode"] == "balanced"
    strengths = [
        player["strength"]
        for side in ("blue", "red")
        for player in launch.snapshot["formation"][side]
    ]
    assert max(strengths) > min(strengths)


def test_failed_launch_can_be_retried_without_reusing_match_id(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    first, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_failed(first.lobby_id, "backend unavailable")

    retried, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-2",
        channel_id=500,
        match_id_factory=lambda: "GF-0043",
    )

    assert should_start is True
    assert retried.status == "pending"
    assert retried.match_id == "GF-0043"
    assert retried.error == ""


def test_successful_launch_blocks_new_interaction_duplicates(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    launch, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_active(launch.lobby_id)

    duplicate, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-2",
        channel_id=500,
        match_id_factory=lambda: "GF-9999",
    )

    assert should_start is False
    assert duplicate.status == "active"
    assert duplicate.match_id == "GF-0042"


def test_operation_id_reuse_by_another_lobby_is_rejected(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    other = replace(_ready_lobby(), lobby_id="lobby-2")

    with pytest.raises(PartyDraftError, match="another lobby"):
        repo.begin(
            other,
            operation_id="interaction-1",
            channel_id=501,
            match_id_factory=lambda: "GF-0043",
        )


def test_active_launch_cannot_be_downgraded_to_failed(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    launch, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_active(launch.lobby_id)

    unchanged = repo.mark_failed(launch.lobby_id, "notification failed")

    assert unchanged.status == "active"
    assert unchanged.error == ""


def test_local_draft_export_retains_party_context():
    lobby = _ready_lobby()
    draft = DraftManager().start(
        500, 1, "Blue", 2, "Red", 10, "Guild", "draft",
        match_id="GF-0042",
        party_context={"lobby_id": lobby.lobby_id, "guild_id": lobby.guild_id},
    )

    assert draft.to_export_dict()["party"] == {
        "lobby_id": "lobby-1",
        "guild_id": 10,
    }


def test_active_launch_creates_authoritative_history_from_actual_contract(
    tmp_path, monkeypatch
):
    import bot

    lobby = _ready_lobby()
    launch_repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    launch, _ = launch_repo.begin(
        lobby,
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    launch = launch_repo.mark_active(lobby.lobby_id)
    history = MatchHistoryRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "match_history_repository", history)

    first = bot._ensure_match_history(lobby, launch)
    retry = bot._ensure_match_history(lobby, launch)

    assert retry == first
    assert first.match_id == first.draft_reference == "GF-0042"
    assert first.organizer_id == 99
    assert first.team_one.captain_id == launch.blue.captain_id
    assert tuple(player.user_id for player in first.team_two.players) == (
        launch.red.participant_ids
    )
    assert {player.role for player in first.participants} == {
        "solo", "jungle", "mid", "support", "adc"
    }
