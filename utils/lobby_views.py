"""Persistent Discord UI primitives for reusable GodForge party lobbies."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord


LOBBY_CARD_CUSTOM_ID_PREFIX = "godforge:lobby:card"
READY_CHECK_CUSTOM_ID_PREFIX = "godforge:lobby:ready-check"
MATCH_RESULT_CUSTOM_ID_PREFIX = "godforge:match:result"
MATCH_CONTINUITY_CUSTOM_ID_PREFIX = "godforge:match:continuity"
MATCH_FORMATION_CUSTOM_ID_PREFIX = "godforge:match:formation"

LOBBY_CARD_ACTIONS = (
    ("join", "Join", discord.ButtonStyle.success),
    ("leave", "Leave", discord.ButtonStyle.secondary),
    ("edit", "Edit", discord.ButtonStyle.primary),
    ("cancel", "Cancel", discord.ButtonStyle.danger),
    ("share", "Share", discord.ButtonStyle.secondary),
    ("ready_check", "Ready Check", discord.ButtonStyle.primary),
    ("teams_role_fit", "Role Fit Teams", discord.ButtonStyle.success),
    ("teams_balanced", "Balanced Teams", discord.ButtonStyle.success),
    ("teams_captains", "Captain Teams", discord.ButtonStyle.primary),
)

READY_CHECK_ACTIONS = (
    ("ready", "Ready", discord.ButtonStyle.success),
    ("need_five", "Need 5 Minutes", discord.ButtonStyle.secondary),
    ("drop", "Drop", discord.ButtonStyle.danger),
)

MATCH_RESULT_ACTIONS = (
    ("team_one", "Blue Won", discord.ButtonStyle.primary),
    ("team_two", "Red Won", discord.ButtonStyle.danger),
    ("cancelled", "Cancel", discord.ButtonStyle.secondary),
    ("no_contest", "No Contest", discord.ButtonStyle.secondary),
)

MATCH_CONTINUITY_ACTIONS = (
    ("run_it_back", "Run It Back", discord.ButtonStyle.success),
    ("shuffle_teams", "Shuffle Teams", discord.ButtonStyle.primary),
    ("return_to_queue", "Return to Queue", discord.ButtonStyle.secondary),
    ("invite_substitutes", "Invite Substitutes", discord.ButtonStyle.secondary),
    ("continue_series", "Continue Series", discord.ButtonStyle.success),
)

MATCH_FORMATION_ACTIONS = (
    ("teams_role_fit", "Role Fit Teams", discord.ButtonStyle.success),
    ("teams_balanced", "Balanced Teams", discord.ButtonStyle.success),
    ("teams_captains", "Captain Teams", discord.ButtonStyle.primary),
    ("move_teams_voice", "Move Teams to Voice", discord.ButtonStyle.secondary),
    ("room_lock", "Lock Rooms", discord.ButtonStyle.secondary),
    ("room_unlock", "Unlock Rooms", discord.ButtonStyle.secondary),
    ("room_close", "Close Rooms", discord.ButtonStyle.danger),
)

ModalHandler = Callable[[discord.Interaction, dict[str, object]], Awaitable[None]]
LobbyActionHandler = Callable[[discord.Interaction, str], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)
_SAFE_ERROR = "GodForge could not complete that action. Please try again."


async def _send_safe_error(
    interaction: discord.Interaction, message: str = _SAFE_ERROR
) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)
    except (discord.HTTPException, AttributeError):
        _LOGGER.exception("Failed to send lobby interaction error")


class _ValidationError(ValueError):
    pass


_MODE_OPTIONS = (
    ("Conquest", "conquest"),
    ("Arena", "arena"),
    ("Joust", "joust"),
)
_REGION_OPTIONS = (
    ("NA East", "na east"),
    ("NA West", "na west"),
    ("Europe", "eu"),
    ("Brazil", "brazil"),
)
_FORMAT_OPTIONS = (
    ("Balanced PUG", "pug"),
    ("Casual Custom", "casual custom"),
    ("Captains Draft", "captains draft"),
    ("Premade Scrim", "premade scrim"),
)
_ROLE_OPTIONS = tuple((role.title(), role) for role in ("solo", "jungle", "mid", "support", "adc"))


def _options(values, selected=None):
    return [
        discord.SelectOption(label=label, value=value, default=value == selected)
        for label, value in values
    ]


class _StateSelect(discord.ui.Select):
    def __init__(self, owner, key: str, *, custom_id: str, placeholder: str, options, row: int):
        super().__init__(
            custom_id=custom_id,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )
        self.owner = owner
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.select(interaction, self.key, self.values[0])


class _WizardButton(discord.ui.Button):
    def __init__(self, owner, action: str, label: str, style, *, custom_id: str, row: int):
        super().__init__(label=label, style=style, custom_id=custom_id, row=row)
        self.owner = owner
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.owner.act(interaction, self.action)
        except _ValidationError as exc:
            await _send_safe_error(interaction, str(exc))
        except Exception:
            _LOGGER.exception("Lobby wizard action failed")
            await _send_safe_error(interaction)


class LobbyNotesModal(discord.ui.Modal):
    """The only free-form input in lobby configuration."""

    def __init__(self, owner) -> None:
        super().__init__(
            title="Lobby Notes",
            custom_id="godforge:lobby:create:notes-modal:v2",
            timeout=900,
        )
        self.owner = owner
        self.notes = discord.ui.TextInput(
            label="Optional notes",
            custom_id="notes",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            default=str(owner.state.get("notes") or ""),
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.owner.state["notes"] = str(self.notes).strip()
        await interaction.response.edit_message(
            embed=self.owner.summary_embed(), view=self.owner
        )


class CreateLobbyWizardView(discord.ui.View):
    """Two-step constrained lobby configuration card."""

    def __init__(self, handler: ModalHandler, initial: dict[str, object] | None = None) -> None:
        super().__init__(timeout=900)
        self._handler = handler
        self.state = dict(initial or {})
        self.page = 1
        self._render()

    def _render(self) -> None:
        self.clear_items()
        if self.page == 1:
            selects = (
                ("mode", "Mode", _MODE_OPTIONS),
                ("region", "Region", _REGION_OPTIONS),
                ("format", "Format", _FORMAT_OPTIONS),
                ("party_size", "Total players", tuple((str(size), str(size)) for size in (2, 4, 6, 8, 10))),
            )
            for row, (key, label, values) in enumerate(selects):
                selected = str(self.state.get(key, "")) or None
                self.add_item(
                    _StateSelect(
                        self,
                        key,
                        custom_id=f"godforge:lobby:create:{'capacity' if key == 'party_size' else key}:v2",
                        placeholder=label,
                        options=_options(values, selected),
                        row=row,
                    )
                )
            self.add_item(
                _WizardButton(
                    self, "next", "Next", discord.ButtonStyle.primary,
                    custom_id="godforge:lobby:create:next:v2", row=4,
                )
            )
            self.add_item(
                _WizardButton(
                    self, "cancel", "Cancel", discord.ButtonStyle.secondary,
                    custom_id="godforge:lobby:create:cancel:v2", row=4,
                )
            )
            return
        voice = "required" if self.state.get("voice_required") is True else (
            "optional" if self.state.get("voice_required") is False else None
        )
        self.add_item(
            _StateSelect(
                self,
                "voice_required",
                custom_id="godforge:lobby:create:voice:v2",
                placeholder="Voice requirement",
                options=_options((("Required", "required"), ("Optional", "optional")), voice),
                row=0,
            )
        )
        skill_values = (
            ("Open / Mixed", "open"),
            ("Beginner", "beginner"),
            ("Casual", "casual"),
            ("Intermediate", "intermediate"),
            ("Experienced", "experienced"),
            ("Competitive", "competitive"),
        )
        self.add_item(
            _StateSelect(
                self,
                "skill_band",
                custom_id="godforge:lobby:create:skill:v2",
                placeholder="Skill band",
                options=_options(skill_values, self.state.get("skill_band")),
                row=1,
            )
        )
        for action, label, style, custom_id in (
            ("notes", "Add Notes", discord.ButtonStyle.secondary, "notes"),
            ("create", "Create Lobby", discord.ButtonStyle.success, "confirm"),
            ("back", "Back", discord.ButtonStyle.secondary, "back"),
            ("cancel", "Cancel", discord.ButtonStyle.danger, "cancel"),
        ):
            self.add_item(
                _WizardButton(
                    self, action, label, style,
                    custom_id=f"godforge:lobby:create:{custom_id}:v2", row=2,
                )
            )

    async def select(self, interaction: discord.Interaction, key: str, value: str) -> None:
        if key == "party_size":
            self.state[key] = int(value)
        elif key == "voice_required":
            self.state[key] = value == "required"
        else:
            self.state[key] = value
        self._render()
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        if action == "cancel":
            await interaction.response.edit_message(
                content="Lobby configuration cancelled.", embed=None, view=None
            )
            return
        if action == "next":
            self._require(("mode", "region", "format", "party_size"))
            self.page = 2
            self._render()
            await interaction.response.edit_message(embed=self.summary_embed(), view=self)
            return
        if action == "back":
            self.page = 1
            self._render()
            await interaction.response.edit_message(embed=self.summary_embed(), view=self)
            return
        if action == "notes":
            await interaction.response.send_modal(LobbyNotesModal(self))
            return
        if action == "create":
            self._require(("mode", "region", "format", "party_size", "voice_required", "skill_band"))
            await self._handler(interaction, self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.state["mode"],
            "region": self.state["region"],
            "format": self.state["format"],
            "party_size": int(self.state["party_size"]),
            "voice_required": bool(self.state["voice_required"]),
            "skill_band": self.state["skill_band"],
            "notes": str(self.state.get("notes") or ""),
        }

    def _require(self, keys) -> None:
        missing = [key.replace("_", " ") for key in keys if key not in self.state]
        if missing:
            raise _ValidationError("Choose " + ", ".join(missing) + " before continuing.")

    def summary_embed(self) -> discord.Embed:
        lines = [
            f"Mode: **{self.state.get('mode', 'not selected')}**",
            f"Region: **{self.state.get('region', 'not selected')}**",
            f"Format: **{self.state.get('format', 'not selected')}**",
            f"Players: **{self.state.get('party_size', 'not selected')}**",
            "Voice: **"
            + ("required" if self.state.get("voice_required") is True else "optional" if self.state.get("voice_required") is False else "not selected")
            + "**",
            f"Skill: **{self.state.get('skill_band', 'not selected')}**",
        ]
        if self.state.get("notes"):
            lines.append(f"Notes: {self.state['notes']}")
        return discord.Embed(
            title=f"Create Lobby · Step {self.page} of 2",
            description="\n".join(lines),
            color=0x3498DB,
        )


class JoinPreferencesView(discord.ui.View):
    """Constrained role, fill, and captain selections for joining a lobby."""

    def __init__(self, handler: ModalHandler, initial: dict[str, object] | None = None) -> None:
        super().__init__(timeout=900)
        self._handler = handler
        self.state = dict(initial or {})
        definitions = (
            ("primary_role", "primary", "Primary role", _ROLE_OPTIONS),
            ("secondary_role", "secondary", "Secondary role", (("None", "none"), *_ROLE_OPTIONS)),
            ("fill", "fill", "Willing to fill?", (("Yes", "yes"), ("No", "no"))),
            ("captain", "captain", "Willing to captain?", (("Yes", "yes"), ("No", "no"))),
        )
        for row, (key, custom_key, label, values) in enumerate(definitions):
            self.add_item(
                _StateSelect(
                    self,
                    key,
                    custom_id=f"godforge:lobby:join:{custom_key}:v2",
                    placeholder=label,
                    options=_options(values),
                    row=row,
                )
            )
        self.add_item(
            _WizardButton(
                self, "confirm", "Join Lobby", discord.ButtonStyle.success,
                custom_id="godforge:lobby:join:confirm:v2", row=4,
            )
        )
        self.add_item(
            _WizardButton(
                self, "cancel", "Cancel", discord.ButtonStyle.secondary,
                custom_id="godforge:lobby:join:cancel:v2", row=4,
            )
        )

    async def select(self, interaction: discord.Interaction, key: str, value: str) -> None:
        if key == "secondary_role":
            self.state[key] = None if value == "none" else value
        elif key in {"fill", "captain"}:
            self.state[key] = value == "yes"
        else:
            self.state[key] = value
        await interaction.response.edit_message(view=self)

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        if action == "cancel":
            await interaction.response.edit_message(
                content="Lobby join cancelled.", view=None
            )
            return
        missing = [key for key in ("primary_role", "secondary_role", "fill", "captain") if key not in self.state]
        if missing:
            raise _ValidationError("Complete every role and availability selection first.")
        if self.state["primary_role"] == self.state["secondary_role"]:
            raise _ValidationError("Primary and secondary roles must be different.")
        await self._handler(interaction, dict(self.state))


class _LobbyActionButton(discord.ui.Button):
    def __init__(
        self,
        action: str,
        label: str,
        style: discord.ButtonStyle,
        handler: LobbyActionHandler,
        *,
        custom_id_prefix: str = LOBBY_CARD_CUSTOM_ID_PREFIX,
        row: int | None = None,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"{custom_id_prefix}:{action}:v1",
            row=row,
        )
        self.action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction, self.action)
        except Exception:
            _LOGGER.exception("Lobby card action failed")
            await _send_safe_error(interaction)


class LobbyCardView(discord.ui.View):
    """Reusable persistent action row for a lobby card message."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for index, (action, label, style) in enumerate(LOBBY_CARD_ACTIONS):
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    row=0 if index < 5 else 1,
                )
            )


class ReadyCheckView(discord.ui.View):
    """Persistent participant responses for an active lobby ready check."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for action, label, style in READY_CHECK_ACTIONS:
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    custom_id_prefix=READY_CHECK_CUSTOM_ID_PREFIX,
                    row=0,
                )
            )


class MatchFormationView(discord.ui.View):
    """Persistent organizer controls hosted by the private match channel."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for index, (action, label, style) in enumerate(MATCH_FORMATION_ACTIONS):
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    custom_id_prefix=MATCH_FORMATION_CUSTOM_ID_PREFIX,
                    row=0 if index < 4 else 1,
                )
            )


class MatchResultView(discord.ui.View):
    """Persistent captain confirmation and organizer resolution controls."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for action, label, style in MATCH_RESULT_ACTIONS:
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    custom_id_prefix=MATCH_RESULT_CUSTOM_ID_PREFIX,
                    row=0,
                )
            )


class MatchContinuityView(discord.ui.View):
    """Persistent organizer controls attached after a confirmed match."""

    def __init__(
        self,
        handler: LobbyActionHandler,
        *,
        allow_continue_series: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        for action, label, style in MATCH_CONTINUITY_ACTIONS:
            if action == "continue_series" and not allow_continue_series:
                continue
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    custom_id_prefix=MATCH_CONTINUITY_CUSTOM_ID_PREFIX,
                    row=0,
                )
            )
