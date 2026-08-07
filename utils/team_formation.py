"""Deterministic, explainable SMITE team formation.

All inputs are owned by GodForge.  No external rank or companion service is
required: organizers may provide a skill band, optional experience, and recent
game-night adjustment alongside the role preferences already stored by parties.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations, permutations
from typing import Iterable, Mapping

SMITE_ROLES = ("solo", "jungle", "mid", "support", "adc")
_BAND_STRENGTH = {
    "new": 1,
    "beginner": 1,
    "casual": 2,
    "intermediate": 3,
    "experienced": 4,
    "competitive": 5,
}


class FormationMode(StrEnum):
    ROLE_FIT = "role_fit"
    BALANCED = "balanced"
    CAPTAINS = "captains"


class TeamFormationError(ValueError):
    """The supplied roster cannot produce two role-complete SMITE teams."""


@dataclass(frozen=True, slots=True)
class FormationPlayer:
    user_id: int
    primary_role: str | None = None
    secondary_role: str | None = None
    fill: bool = False
    captain: bool = False
    skill_band: str = ""
    experience: int = 0
    recent_adjustment: int = 0

    def __post_init__(self) -> None:
        primary = _role(self.primary_role)
        secondary = _role(self.secondary_role)
        if primary == secondary:
            secondary = None
        if self.experience < 0:
            raise TeamFormationError("experience cannot be negative")
        object.__setattr__(self, "primary_role", primary)
        object.__setattr__(self, "secondary_role", secondary)
        object.__setattr__(self, "skill_band", self.skill_band.strip().lower())

    @property
    def strength(self) -> int:
        band = _BAND_STRENGTH.get(self.skill_band, 3)
        # Experience is deliberately bounded so self-reported history informs
        # balance without overwhelming the organizer's skill band.
        return band * 100 + min(self.experience, 100) + self.recent_adjustment


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    user_id: int
    role: str
    preference: str
    strength: int


@dataclass(frozen=True, slots=True)
class FormedTeam:
    captain_id: int
    assignments: tuple[RoleAssignment, ...]

    @property
    def participant_ids(self) -> tuple[int, ...]:
        return tuple(assignment.user_id for assignment in self.assignments)

    @property
    def strength(self) -> int:
        return sum(assignment.strength for assignment in self.assignments)


@dataclass(frozen=True, slots=True)
class FormationResult:
    mode: FormationMode
    blue: FormedTeam
    red: FormedTeam
    first_choices: int
    second_choices: int
    fills: int
    strength_difference: int
    draft_order: tuple[int, ...] = ()

    @property
    def explanation(self) -> str:
        text = (
            f"{self.first_choices} first choices, {self.second_choices} second "
            f"choices, {self.fills} unavoidable fills; team strength difference "
            f"{self.strength_difference}."
        )
        if self.mode is FormationMode.CAPTAINS:
            text += " Captain picks alternated in snake order with role visibility."
        return text


def form_smite_teams(
    players: Iterable[FormationPlayer],
    mode: FormationMode | str = FormationMode.ROLE_FIT,
) -> FormationResult:
    """Form equal teams from an even roster of two through ten players."""
    roster = tuple(sorted(players, key=lambda player: player.user_id))
    mode = FormationMode(mode)
    if len(roster) % 2:
        raise TeamFormationError(
            "odd rosters cannot enter formation; wait for one player, drop one "
            "player, or use a substitute"
        )
    if len(roster) < 2 or len(roster) > 10:
        raise TeamFormationError(
            "team formation requires an even roster from two through ten players"
        )
    if len({player.user_id for player in roster}) != len(roster):
        raise TeamFormationError("player IDs must be unique")
    if mode is FormationMode.CAPTAINS:
        return _captain_draft(roster)
    return _optimized_split(roster, mode)


def _optimized_split(
    roster: tuple[FormationPlayer, ...], mode: FormationMode
) -> FormationResult:
    by_id = {player.user_id: player for player in roster}
    team_size = len(roster) // 2
    assignments: dict[
        frozenset[int], tuple[int, tuple[int, int, int], FormedTeam]
    ] = {}
    for members in combinations(roster, team_size):
        team = _best_role_assignment(members)
        archetype_penalty = _archetype_penalty(team)
        assignments[frozenset(player.user_id for player in members)] = (
            archetype_penalty,
            _fit_totals(team),
            team,
        )

    all_ids = frozenset(by_id)
    captain_ids = {player.user_id for player in roster if player.captain}
    candidates = []
    for blue_ids, (blue_archetype, blue_fit, blue) in assignments.items():
        # The smallest player ID is always blue, removing mirrored duplicates.
        if min(all_ids) not in blue_ids:
            continue
        red_archetype, red_fit, red = assignments[all_ids - blue_ids]
        archetype_penalty = blue_archetype + red_archetype
        fit = tuple(a + b for a, b in zip(blue_fit, red_fit))
        difference = abs(blue.strength - red.strength)
        captain_penalty = int(
            len(captain_ids) >= 2
            and (not captain_ids.intersection(blue_ids) or not captain_ids.intersection(all_ids - blue_ids))
        )
        # Composition and role satisfaction remain ahead of the transparent
        # GodForge-owned strength score. The selected mode remains part of the
        # durable explanation; identical optimal inputs may intentionally yield
        # the same teams in role-fit and balanced modes.
        key = (
            archetype_penalty,
            fit,
            captain_penalty,
            difference,
            _identity_key(blue, red),
        )
        candidates.append((key, blue, red, fit, difference))

    _, blue, red, fit, difference = min(candidates, key=lambda item: item[0])
    return FormationResult(
        mode,
        blue,
        red,
        first_choices=-fit[0],
        second_choices=fit[1],
        fills=fit[2],
        strength_difference=difference,
    )


def _captain_draft(roster: tuple[FormationPlayer, ...]) -> FormationResult:
    eligible = [player for player in roster if player.captain]
    if len(eligible) < 2:
        raise TeamFormationError("captain mode requires two captain volunteers")
    # Strongest eligible captains are separated; ID is a stable tie-breaker.
    captains = sorted(eligible, key=lambda p: (-p.strength, p.user_id))[:2]
    blue_players = [captains[0]]
    red_players = [captains[1]]
    remaining = [player for player in roster if player not in captains]
    draft_order: list[int] = []
    # B, R, R, B repeating is a deterministic snake draft.
    sides = (blue_players, red_players, red_players, blue_players) * 2
    for side in sides:
        if not remaining:
            break
        other = red_players if side is blue_players else blue_players
        choice = min(
            remaining,
            key=lambda player: _captain_pick_key(side, other, player),
        )
        side.append(choice)
        remaining.remove(choice)
        draft_order.append(choice.user_id)
    blue = _best_role_assignment(tuple(blue_players), captain_id=captains[0].user_id)
    red = _best_role_assignment(tuple(red_players), captain_id=captains[1].user_id)
    fit = tuple(a + b for a, b in zip(_fit_totals(blue), _fit_totals(red)))
    return FormationResult(
        FormationMode.CAPTAINS,
        blue,
        red,
        first_choices=-fit[0],
        second_choices=fit[1],
        fills=fit[2],
        strength_difference=abs(blue.strength - red.strength),
        draft_order=tuple(draft_order),
    )


def _captain_pick_key(
    side: list[FormationPlayer],
    other: list[FormationPlayer],
    candidate: FormationPlayer,
) -> tuple:
    projected = tuple(side + [candidate])
    # Until a side has five players, estimate fit from distinct preferred roles.
    covered = {role for p in projected for role in (p.primary_role, p.secondary_role) if role}
    strength_gap = abs(sum(p.strength for p in projected) - sum(p.strength for p in other))
    return (-len(covered), strength_gap, candidate.user_id)


def _best_role_assignment(
    players: tuple[FormationPlayer, ...], *, captain_id: int | None = None
) -> FormedTeam:
    if not 1 <= len(players) <= 5:
        raise TeamFormationError("each SMITE team requires one through five players")
    players = tuple(sorted(players, key=lambda player: player.user_id))
    candidates = []
    for roles in permutations(SMITE_ROLES, len(players)):
        assignments = tuple(
            RoleAssignment(
                player.user_id,
                role,
                _preference(player, role),
                player.strength,
            )
            for player, role in zip(players, roles)
        )
        fit = _fit_totals_from_assignments(assignments)
        candidates.append(
            (
                _archetype_penalty_from_assignments(assignments),
                fit,
                tuple((a.user_id, a.role) for a in assignments),
                assignments,
            )
        )
    _, _, _, best = min(candidates)
    captain = captain_id or next(
        (
            assignment.user_id
            for assignment in best
            if _player(players, assignment.user_id).captain
        ),
        best[0].user_id,
    )
    return FormedTeam(captain, best)


def _preference(player: FormationPlayer, role: str) -> str:
    if role == player.primary_role:
        return "first"
    if role == player.secondary_role:
        return "second"
    return "fill"


def _fit_totals(team: FormedTeam) -> tuple[int, int, int]:
    return _fit_totals_from_assignments(team.assignments)


def _archetype_penalty(team: FormedTeam) -> int:
    return _archetype_penalty_from_assignments(team.assignments)


def _archetype_penalty_from_assignments(
    assignments: tuple[RoleAssignment, ...],
) -> int:
    team_size = len(assignments)
    target_tanks = {1: None, 2: 1, 3: 1, 4: 2, 5: 2}[team_size]
    if target_tanks is None:
        return 0
    tanks = sum(assignment.role in {"solo", "support"} for assignment in assignments)
    return abs(tanks - target_tanks)


def _fit_totals_from_assignments(
    assignments: tuple[RoleAssignment, ...],
) -> tuple[int, int, int]:
    first = sum(a.preference == "first" for a in assignments)
    second = sum(a.preference == "second" for a in assignments)
    fills = len(assignments) - first - second
    return (-first, -second, fills)


def _identity_key(blue: FormedTeam, red: FormedTeam) -> tuple:
    return (blue.participant_ids, red.participant_ids)


def _player(players: tuple[FormationPlayer, ...], user_id: int) -> FormationPlayer:
    return next(player for player in players if player.user_id == user_id)


def _role(value: str | None) -> str | None:
    role = str(value or "").strip().lower()
    if not role:
        return None
    if role not in SMITE_ROLES:
        raise TeamFormationError(f"unknown SMITE role: {role}")
    return role


def profiles_from_preferences(
    participants: Iterable[object],
    *,
    organizer_inputs: Mapping[int, Mapping[str, object]] | None = None,
) -> tuple[FormationPlayer, ...]:
    """Build profiles from party preferences plus optional organizer-owned data."""
    organizer_inputs = organizer_inputs or {}
    profiles = []
    for participant in participants:
        user_id = int(getattr(participant, "user_id"))
        inputs = organizer_inputs.get(user_id, {})
        profiles.append(
            FormationPlayer(
                user_id,
                primary_role=getattr(participant, "primary_role", None),
                secondary_role=getattr(participant, "secondary_role", None),
                fill=bool(getattr(participant, "fill", False)),
                captain=bool(getattr(participant, "captain", False)),
                skill_band=str(inputs.get("skill_band", "")),
                experience=int(inputs.get("experience", 0)),
                recent_adjustment=int(inputs.get("recent_adjustment", 0)),
            )
        )
    return tuple(profiles)
