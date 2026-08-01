"""The `/party room` organizer-controls subcommand: Discord adapter.

Feature module for the Issue #48 refactor. Owns the `room` subcommand
(lock/unlock/remove/transfer/move/close), previously defined directly in
``bot.py``. Domain logic stays in ``utils/match_rooms.py``; this module routes
one Discord command to the appropriate ``MatchRoomService`` call.

Registered onto the existing `/party` command group via
``register_party_room_command``, mirroring the schedule-commands pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands


@dataclass
class PartyRoomCommandDeps:
    """Collaborators injected into the `/party room` command."""

    party_repository: object
    match_room_service_for_guild: Callable


def register_party_room_command(
    group: app_commands.Group, deps: PartyRoomCommandDeps
) -> None:
    """Register `room` onto the existing `/party` group.

    Also attached as ``group.room`` for direct invocation in tests.
    """

    @group.command(
        name="room",
        description="Use organizer controls for a ready lobby's temporary rooms",
    )
    @app_commands.describe(
        lobby_id="Stable lobby ID shown on the lobby card",
        action="Select a guided room action",
        member="Player used by remove, transfer, or move",
        lobby_voice="Configured source voice room for move",
        team="Destination team number for move",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Lock", value="lock"),
            app_commands.Choice(name="Unlock", value="unlock"),
            app_commands.Choice(name="Remove Player", value="remove"),
            app_commands.Choice(name="Transfer Organizer", value="transfer"),
            app_commands.Choice(name="Move to Team", value="move"),
            app_commands.Choice(name="Close Rooms", value="close"),
        ],
        team=[
            app_commands.Choice(name="Team 1", value=1),
            app_commands.Choice(name="Team 2", value=2),
        ],
    )
    async def room(
        interaction: discord.Interaction,
        lobby_id: str,
        action: str,
        member: discord.Member | None = None,
        lobby_voice: discord.VoiceChannel | None = None,
        team: int | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        service = deps.match_room_service_for_guild(interaction.guild)
        actor_id = interaction.user.id
        action = action.strip().lower()
        try:
            if action == "lock":
                rooms = await service.lock(lobby_id, actor_id=actor_id)
            elif action == "unlock":
                rooms = await service.unlock(lobby_id, actor_id=actor_id)
            elif action == "remove" and member is not None:
                rooms = await service.remove_player(
                    lobby_id, actor_id=actor_id, user_id=member.id
                )
            elif action == "transfer" and member is not None:
                rooms = await service.transfer_transactionally(
                    lobby_id,
                    actor_id=actor_id,
                    new_organizer_id=member.id,
                    commit=lambda: deps.party_repository.transfer_organizer(
                        interaction.guild.id,
                        lobby_id,
                        member.id,
                        operation_id=f"discord:{interaction.id}:room-transfer",
                        actor_id=actor_id,
                    ),
                    compensate=lambda: deps.party_repository.transfer_organizer(
                        interaction.guild.id,
                        lobby_id,
                        actor_id,
                        operation_id=(
                            f"discord:{interaction.id}:room-transfer-compensation"
                        ),
                        actor_id=member.id,
                    ),
                )
            elif (
                action == "move"
                and member is not None
                and lobby_voice is not None
                and team is not None
            ):
                failures = await service.move_players(
                    lobby_id,
                    actor_id=actor_id,
                    lobby_voice_id=lobby_voice.id,
                    team_assignments={member.id: team},
                )
                if failures:
                    await interaction.response.send_message(
                        failures[member.id], ephemeral=True
                    )
                    return
                rooms = await service.get(lobby_id)
            elif action == "close":
                rooms = await service.close(
                    lobby_id, actor_id=actor_id, reason="organizer closed rooms"
                )
            else:
                await interaction.response.send_message(
                    "Use lock, unlock, remove, transfer, move, or close. "
                    "Player/team inputs are required for their matching actions.",
                    ephemeral=True,
                )
                return
        except (LookupError, PermissionError, ValueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except RuntimeError:
            await interaction.response.send_message(
                "GodForge could not complete that room action. Check the saved room "
                "state and Discord permissions, then retry.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Room action `{action}` completed for `{rooms.lobby_id[:8]}`.",
            ephemeral=True,
        )

    group.room = room
