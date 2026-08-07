"""Domain model for durable GodForge party lobbies.

Discord IDs are delivery references only.  ``lobby_id`` is the stable domain
identity and survives message, channel, and room recreation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class LobbyState(StrEnum):
    OPEN = "open"
    FULL = "full"
    READY_CHECK = "ready_check"
    FORMING = "forming"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {LobbyState.COMPLETED, LobbyState.CANCELLED, LobbyState.EXPIRED}
)
ACTIVE_STATES = frozenset(LobbyState) - TERMINAL_STATES

# Backward edges are deliberate: a player leaving or declining a ready check
# re-opens recruitment without replacing the lobby's domain identity.
ALLOWED_TRANSITIONS = MappingProxyType(
    {
        LobbyState.OPEN: frozenset(
            {
                LobbyState.FULL,
                LobbyState.READY_CHECK,
                LobbyState.CANCELLED,
                LobbyState.EXPIRED,
            }
        ),
        LobbyState.FULL: frozenset(
            {
                LobbyState.OPEN,
                LobbyState.READY_CHECK,
                LobbyState.CANCELLED,
                LobbyState.EXPIRED,
            }
        ),
        LobbyState.READY_CHECK: frozenset(
            {
                LobbyState.OPEN,
                LobbyState.FULL,
                LobbyState.FORMING,
                LobbyState.CANCELLED,
                LobbyState.EXPIRED,
            }
        ),
        LobbyState.FORMING: frozenset(
            {LobbyState.READY_CHECK, LobbyState.ACTIVE, LobbyState.CANCELLED}
        ),
        LobbyState.ACTIVE: frozenset(
            {LobbyState.COMPLETED, LobbyState.CANCELLED}
        ),
        LobbyState.COMPLETED: frozenset(),
        LobbyState.CANCELLED: frozenset(),
        LobbyState.EXPIRED: frozenset(),
    }
)


class InvalidLobbyTransition(ValueError):
    """Raised when a requested lifecycle transition is not permitted."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def validate_transition(current: LobbyState, target: LobbyState) -> None:
    """Validate a transition; transitioning to the current state is idempotent."""
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidLobbyTransition(f"cannot transition lobby from {current} to {target}")


@dataclass(frozen=True, slots=True)
class DiscordDelivery:
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    voice_channel_id: int | None = None
    team_channel_ids: tuple[int, ...] = ()
    match_channel_id: int | None = None
    formation_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class PlayerPreferences:
    primary_role: str | None = None
    secondary_role: str | None = None
    fill: bool = False
    captain: bool = False

    def __post_init__(self) -> None:
        primary = _normalize_optional(self.primary_role)
        secondary = _normalize_optional(self.secondary_role)
        if secondary == primary:
            secondary = None
        object.__setattr__(self, "primary_role", primary)
        object.__setattr__(self, "secondary_role", secondary)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(role for role in (self.primary_role, self.secondary_role) if role)

    def __eq__(self, other) -> bool:
        # Preserve compatibility with the original tuple-based repository
        # interface while callers migrate to the authoritative named fields.
        if isinstance(other, (tuple, list)):
            return self.roles == tuple(other)
        if isinstance(other, PlayerPreferences):
            return (
                self.primary_role,
                self.secondary_role,
                self.fill,
                self.captain,
            ) == (
                other.primary_role,
                other.secondary_role,
                other.fill,
                other.captain,
            )
        return NotImplemented


def _normalize_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


@dataclass(frozen=True, slots=True)
class Participant:
    user_id: int
    preferences: tuple[str, ...] = ()
    ready: bool = False
    joined_at: datetime = field(default_factory=utc_now)
    primary_role: str | None = None
    secondary_role: str | None = None
    fill: bool = False
    captain: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "joined_at", ensure_utc(self.joined_at))
        legacy = tuple(
            dict.fromkeys(p.strip().lower() for p in self.preferences if p.strip())
        )
        profile = PlayerPreferences(
            self.primary_role or (legacy[0] if legacy else None),
            self.secondary_role or (legacy[1] if len(legacy) > 1 else None),
            self.fill,
            self.captain,
        )
        object.__setattr__(self, "primary_role", profile.primary_role)
        object.__setattr__(self, "secondary_role", profile.secondary_role)
        object.__setattr__(self, "fill", profile.fill)
        object.__setattr__(self, "captain", profile.captain)
        object.__setattr__(self, "preferences", profile.roles)


@dataclass(frozen=True, slots=True)
class PartyLobby:
    lobby_id: str
    guild_id: int
    organizer_id: int
    capacity: int
    state: LobbyState = LobbyState.OPEN
    participants: tuple[Participant, ...] = ()
    delivery: DiscordDelivery = field(default_factory=DiscordDelivery)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    version: int = 1
    mode: str = "standard"
    region: str = ""
    format: str = "5v5"
    voice_required: bool = False
    skill_band: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.lobby_id.strip():
            raise ValueError("lobby_id is required")
        if self.capacity < 2:
            raise ValueError("capacity must be at least 2")
        if len({p.user_id for p in self.participants}) != len(self.participants):
            raise ValueError("participant user IDs must be unique")
        if len(self.participants) > self.capacity:
            raise ValueError("participants exceed lobby capacity")
        object.__setattr__(self, "state", LobbyState(self.state))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        object.__setattr__(self, "mode", _normalize_optional(self.mode) or "standard")
        object.__setattr__(self, "region", _normalize_optional(self.region) or "")
        object.__setattr__(self, "format", _normalize_optional(self.format) or "5v5")
        object.__setattr__(self, "skill_band", _normalize_optional(self.skill_band) or "")
        object.__setattr__(self, "notes", self.notes.strip())

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def participant(self, user_id: int) -> Participant | None:
        return next((p for p in self.participants if p.user_id == user_id), None)

    def transitioned(self, target: LobbyState, *, at: datetime | None = None) -> "PartyLobby":
        validate_transition(self.state, target)
        if self.state == target:
            return self
        return replace(
            self,
            state=target,
            updated_at=ensure_utc(at) or utc_now(),
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    lobby_id: str
    guild_id: int
    operation_id: str
    event_type: str
    from_state: LobbyState | None
    to_state: LobbyState
    actor_id: int | None
    occurred_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    lobby: PartyLobby
    """An active lobby and all data needed for Discord-side reconciliation."""

