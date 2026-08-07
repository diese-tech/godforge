import pytest

from utils.team_formation import (
    FormationMode,
    FormationPlayer,
    SMITE_ROLES,
    TeamFormationError,
    form_smite_teams,
)


def _roster(*, captains=(1, 6)):
    roles = SMITE_ROLES * 2
    return tuple(
        FormationPlayer(
            user_id=index,
            primary_role=role,
            secondary_role=SMITE_ROLES[(SMITE_ROLES.index(role) + 1) % 5],
            captain=index in captains,
            skill_band="intermediate",
            experience=index,
        )
        for index, role in enumerate(roles, start=1)
    )


def test_role_fit_gives_common_roster_all_first_choices():
    result = form_smite_teams(_roster(), FormationMode.ROLE_FIT)

    assert result.first_choices == 10
    assert result.second_choices == 0
    assert result.fills == 0
    assert {a.role for a in result.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in result.red.assignments} == set(SMITE_ROLES)


def test_two_player_roster_forms_deterministic_one_player_sides():
    roster = (
        FormationPlayer(1, primary_role="solo", captain=True),
        FormationPlayer(2, primary_role="mid", captain=True),
    )

    first = form_smite_teams(roster, FormationMode.ROLE_FIT)
    second = form_smite_teams(reversed(roster), FormationMode.ROLE_FIT)

    assert first == second
    assert len(first.blue.assignments) == len(first.red.assignments) == 1
    assert set(first.blue.participant_ids + first.red.participant_ids) == {1, 2}
    assert first.first_choices == 2
    assert first.fills == 0


@pytest.mark.parametrize(
    ("team_roles", "expected_tanks"),
    [
        (("solo",), None),
        (("solo", "mid"), 1),
        (("solo", "mid", "adc"), 1),
        (("solo", "support", "mid", "adc"), 2),
        (SMITE_ROLES, 2),
    ],
)
def test_every_supported_roster_size_uses_the_preferred_archetype(
    team_roles, expected_tanks
):
    roles = team_roles * 2
    roster = tuple(
        FormationPlayer(index, primary_role=role, captain=index in {1, len(team_roles) + 1})
        for index, role in enumerate(roles, start=1)
    )

    result = form_smite_teams(roster, FormationMode.ROLE_FIT)

    assert len(result.blue.assignments) == len(result.red.assignments) == len(team_roles)
    if expected_tanks is not None:
        for team in (result.blue, result.red):
            assert sum(a.role in {"solo", "support"} for a in team.assignments) == expected_tanks
    if len(team_roles) == 5:
        assert {a.role for a in result.blue.assignments} == set(SMITE_ROLES)
        assert {a.role for a in result.red.assignments} == set(SMITE_ROLES)


def test_adversarial_roster_reports_unavoidable_fills_deterministically():
    roster = tuple(
        FormationPlayer(index, primary_role="mid", fill=True)
        for index in range(1, 11)
    )

    first = form_smite_teams(roster, FormationMode.ROLE_FIT)
    second = form_smite_teams(reversed(roster), FormationMode.ROLE_FIT)

    assert first == second
    assert first.first_choices == 2
    assert first.fills == 8
    assert "8 unavoidable fills" in first.explanation


def test_two_player_sides_with_only_dps_preferences_report_required_tank_fills():
    roster = tuple(
        FormationPlayer(index, primary_role="mid", fill=True, captain=index in {1, 3})
        for index in range(1, 5)
    )

    result = form_smite_teams(roster, FormationMode.ROLE_FIT)

    assert result.fills == 2
    for team in (result.blue, result.red):
        assert sum(a.role in {"solo", "support"} for a in team.assignments) == 1
        assert sum(a.role in {"jungle", "mid", "adc"} for a in team.assignments) == 1
    assert "2 unavoidable fills" in result.explanation


def test_balanced_mode_preserves_role_satisfaction_before_strength_tiebreak():
    roster = tuple(
        FormationPlayer(
            index,
            primary_role=SMITE_ROLES[(index - 1) % 5],
            fill=True,
            skill_band="competitive" if index <= 5 else "beginner",
            experience=index,
        )
        for index in range(1, 11)
    )

    result = form_smite_teams(roster, FormationMode.BALANCED)

    assert result.first_choices == 10
    assert result.strength_difference == 395
    assert "team strength difference" in result.explanation


def test_captain_mode_separates_volunteers_and_exposes_snake_order():
    result = form_smite_teams(_roster(), FormationMode.CAPTAINS)

    assert {result.blue.captain_id, result.red.captain_id} == {1, 6}
    assert len(result.draft_order) == 8
    assert set(result.draft_order) == set(range(1, 11)) - {1, 6}
    assert {a.role for a in result.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in result.red.assignments} == set(SMITE_ROLES)


@pytest.mark.parametrize("roster_size", (2, 4, 6, 8, 10))
def test_captain_mode_keeps_equal_sides_for_every_supported_roster(roster_size):
    roster = tuple(
        FormationPlayer(
            index,
            primary_role=SMITE_ROLES[(index - 1) % len(SMITE_ROLES)],
            captain=index in {1, 2},
        )
        for index in range(1, roster_size + 1)
    )

    result = form_smite_teams(roster, FormationMode.CAPTAINS)

    assert len(result.blue.assignments) == len(result.red.assignments) == roster_size // 2
    assert {result.blue.captain_id, result.red.captain_id} == {1, 2}


def test_captain_mode_requires_two_volunteers():
    with pytest.raises(TeamFormationError, match="two captain"):
        form_smite_teams(_roster(captains=(1,)), FormationMode.CAPTAINS)


def test_roster_validation_rejects_odd_and_duplicate_players():
    with pytest.raises(TeamFormationError, match="wait for one player"):
        form_smite_teams(_roster()[:9])
    duplicated = _roster()[:-1] + (_roster()[0],)
    with pytest.raises(TeamFormationError, match="unique"):
        form_smite_teams(duplicated)


@pytest.mark.parametrize("seed", range(25))
def test_simulated_mixed_rosters_are_role_complete_and_repeatable(seed):
    roster = tuple(
        FormationPlayer(
            user_id=index + 1,
            primary_role=SMITE_ROLES[(index * 3 + seed) % 5],
            secondary_role=SMITE_ROLES[(index * 3 + seed + 1) % 5],
            fill=index % 3 == 0,
            captain=index in {seed % 10, (seed + 5) % 10},
            skill_band=("beginner", "intermediate", "competitive")[
                (index + seed) % 3
            ],
            experience=(index * 7 + seed) % 40,
            recent_adjustment=(index + seed) % 5 - 2,
        )
        for index in range(10)
    )
    first = form_smite_teams(roster, FormationMode.BALANCED)
    second = form_smite_teams(reversed(roster), FormationMode.BALANCED)

    assert first == second
    assert {a.role for a in first.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in first.red.assignments} == set(SMITE_ROLES)
