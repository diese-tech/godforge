"""The Issue #67 public RSVP card for scheduled Party sessions.

Feature module: owns the RSVP card embed, its auto-publish/reconcile/
repost-on-deletion lifecycle (mirroring how ``PartyLobbyService`` treats the
public queue card), the weekly rollover that keeps one Discord message across
occurrences instead of reposting, and the button-first Going/Maybe/Can't Make
It flow. Domain state lives in ``utils.party_schedule``; this module is the
Discord-facing adapter, following the same "inject collaborators, stay
Discord-UI-agnostic in the constructor" convention as ``PartyLobbyService``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord

from utils.party import PlayerPreferences
from utils.party_schedule import EventState, Recurrence, ScheduledNight

ROSTER_PREVIEW_LIMIT = 6


@dataclass
class ScheduleRsvpDeps:
    """Collaborators injected into `ScheduleRsvpService`."""

    schedule_repository: object
    party_repository: object
    settings_module: object
    log: object
    rsvp_view: Callable[[], discord.ui.View]
    join_preferences_view: Callable[..., discord.ui.View]
    change_roles_view: Callable[..., discord.ui.View]


class ScheduleRsvpService:
    def __init__(self, deps: ScheduleRsvpDeps) -> None:
        self.deps = deps

    # -- Rendering ----------------------------------------------------------

    @staticmethod
    def _rsvp_role_summary(rsvp, guild: discord.Guild | None = None) -> str:
        member = guild.get_member(rsvp.user_id) if guild is not None else None
        label = member.display_name if member is not None else f"<@{rsvp.user_id}>"
        roles = [
            role.title()
            for role in (rsvp.preferences.primary_role, rsvp.preferences.secondary_role)
            if role
        ]
        if roles:
            role_text = " / ".join(roles)
            if rsvp.preferences.fill:
                role_text += " (Fill)"
        else:
            role_text = "Fill" if rsvp.preferences.fill else "No role set"
        return f"{label} · {role_text}"

    def rsvp_embed(self, event: ScheduledNight, guild: discord.Guild | None = None) -> discord.Embed:
        recurrence_label = "Weekly" if event.recurrence is Recurrence.WEEKLY else "One-time"
        timestamp = int(event.starts_at.timestamp())
        embed = discord.Embed(
            title=event.title,
            description=(
                f"{recurrence_label} custom night\n"
                f"Next session: <t:{timestamp}:F> (<t:{timestamp}:R>)"
            ),
            color=0x9B59B6,
        )
        going_lines = [
            self._rsvp_role_summary(rsvp, guild) for rsvp in event.rsvps[:ROSTER_PREVIEW_LIMIT]
        ]
        if len(event.rsvps) > ROSTER_PREVIEW_LIMIT:
            going_lines.append(f"+{len(event.rsvps) - ROSTER_PREVIEW_LIMIT} others")
        embed.add_field(
            name=f"Going · {len(event.rsvps)}/{event.capacity}",
            value="\n".join(going_lines) or "No one yet.",
            inline=False,
        )
        if event.waitlist:
            embed.add_field(
                name=f"Waitlist · {len(event.waitlist)}",
                value="\n".join(
                    f"<@{rsvp.user_id}>" for rsvp in event.waitlist[:ROSTER_PREVIEW_LIMIT]
                ),
                inline=False,
            )
        if event.maybe:
            embed.add_field(
                name=f"Maybe · {len(event.maybe)}",
                value="\n".join(
                    f"<@{rsvp.user_id}>" for rsvp in event.maybe[:ROSTER_PREVIEW_LIMIT]
                ),
                inline=False,
            )
        if event.state is EventState.CONVERTED:
            status = "This session has started."
            lobby = (
                self.deps.party_repository.get(event.guild_id, event.lobby_id)
                if event.lobby_id else None
            )
            if lobby is not None and lobby.delivery.panel_channel_id:
                status += f" Continue in <#{lobby.delivery.panel_channel_id}>."
            embed.add_field(name="Status", value=status, inline=False)
        elif event.state is EventState.CANCELLED:
            embed.add_field(name="Status", value="This session was cancelled.", inline=False)
        embed.set_footer(text=f"event_id={event.event_id}")
        return embed

    @staticmethod
    def _accepts_rsvps(event: ScheduledNight) -> bool:
        return event.state is EventState.SCHEDULED

    def event_id_from_interaction(self, interaction: discord.Interaction) -> str:
        """Read the event_id stamped in an RSVP card's embed footer.

        Mirrors ``PartyLobbyService.lobby_id_from_interaction`` — identity
        lookup only, safe against any message carrying the footer.
        """
        embeds = getattr(interaction.message, "embeds", ())
        footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
        if not footer.startswith("event_id="):
            raise ValueError("This RSVP card is missing its stable identity.")
        return footer.removeprefix("event_id=")

    # -- Card lifecycle -------------------------------------------------------

    async def refresh_rsvp_card(self, event: ScheduledNight, guild) -> ScheduledNight:
        """Best-effort refresh of the durable public RSVP card, if one exists."""
        if guild is None or not event.delivery_channel_id or not event.delivery_message_id:
            return event
        channel = guild.get_channel(event.delivery_channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return event
        try:
            message = await channel.fetch_message(event.delivery_message_id)
            await message.edit(
                embed=self.rsvp_embed(event, guild),
                view=self.deps.rsvp_view() if self._accepts_rsvps(event) else None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            self.deps.log.exception(
                "Could not refresh scheduled-night RSVP card %s", event.event_id
            )
        return event

    async def ensure_rsvp_card(self, event: ScheduledNight, guild) -> tuple[ScheduledNight, bool]:
        """Refresh the RSVP card if it exists, else publish it fresh.

        Same shape as ``PartyLobbyService.ensure_public_lobby_card``: covers
        both auto-publish on confirm and repost-after-manual-deletion with
        one code path. Returns ``(event, True)`` when a new message was
        posted, ``(event, False)`` otherwise.
        """
        if guild is None:
            return event, False
        if event.delivery_channel_id and event.delivery_message_id:
            channel = guild.get_channel(event.delivery_channel_id)
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    await channel.fetch_message(event.delivery_message_id)
                    await self.refresh_rsvp_card(event, guild)
                    return event, False
                except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                        AttributeError):
                    pass  # Stored message is gone; fall through and repost.
        deps = self.deps
        guild_settings = deps.settings_module.get_guild_settings(str(event.guild_id))
        channel_id = guild_settings["managed"].get("playChannelId")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None or not hasattr(channel, "send"):
            return event, False
        message = await channel.send(
            embed=self.rsvp_embed(event, guild),
            view=deps.rsvp_view() if self._accepts_rsvps(event) else None,
        )
        updated = deps.schedule_repository.set_delivery(event.event_id, channel.id, message.id)
        return updated, True

    # -- Button flow ------------------------------------------------------

    async def handle_rsvp_action(self, interaction: discord.Interaction, action: str) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        event_id = self.event_id_from_interaction(interaction)
        event = deps.schedule_repository.get(event_id)
        if event is None or event.guild_id != guild_id or not self._accepts_rsvps(event):
            await interaction.response.send_message(
                "This scheduled night is no longer accepting RSVPs.", ephemeral=True,
            )
            return
        if action == "cant_make_it":
            changed = deps.schedule_repository.cancel_rsvp(event.event_id, interaction.user.id)
            await self.refresh_rsvp_card(changed, interaction.guild)
            await interaction.response.send_message("RSVP removed.", ephemeral=True)
            return
        saved = deps.party_repository.get_player_preferences(guild_id, interaction.user.id)
        if action == "maybe":
            changed = deps.schedule_repository.rsvp_maybe(
                event.event_id, interaction.user.id, saved
            )
            await self.refresh_rsvp_card(changed, interaction.guild)
            await interaction.response.send_message("Marked as Maybe.", ephemeral=True)
            return
        if action == "going":
            await self._handle_going(interaction, event, saved)
            return

    async def _handle_going(self, interaction, event: ScheduledNight, saved) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if saved.primary_role:
            changed = deps.schedule_repository.rsvp(event.event_id, interaction.user.id, saved)
            await self.refresh_rsvp_card(changed, interaction.guild)
            roles = "/".join(r for r in (saved.primary_role, saved.secondary_role) if r)

            async def change_roles(cr_interaction: discord.Interaction) -> None:
                await self._open_role_wizard(cr_interaction, event)

            await interaction.response.send_message(
                f"You're Going to **{event.title}** ({roles}).",
                view=deps.change_roles_view(change_roles),
                ephemeral=True,
            )
            return
        await self._open_role_wizard(interaction, event)

    async def _open_role_wizard(self, interaction: discord.Interaction, event: ScheduledNight) -> None:
        deps = self.deps
        guild_id = interaction.guild_id

        async def going_handler(wizard_interaction: discord.Interaction, payload: dict) -> None:
            current = deps.party_repository.get_player_preferences(
                guild_id, wizard_interaction.user.id
            )
            preferences = PlayerPreferences(
                str(payload["primary_role"]),
                str(payload.get("secondary_role") or "") or None,
                bool(payload["fill"]),
                current.captain,
            )
            deps.party_repository.set_player_preferences(
                guild_id, wizard_interaction.user.id, preferences
            )
            changed = deps.schedule_repository.rsvp(
                event.event_id, wizard_interaction.user.id, preferences
            )
            if wizard_interaction.guild is not None:
                await self.refresh_rsvp_card(changed, wizard_interaction.guild)
            await wizard_interaction.response.send_message(
                f"You're Going to **{event.title}**!", ephemeral=True,
            )

        await interaction.response.send_message(
            f"Choose your role preferences for **{event.title}**.",
            view=deps.join_preferences_view(going_handler),
            ephemeral=True,
        )

    # -- Reconciliation -----------------------------------------------------

    async def reconcile(self, event: ScheduledNight, ctx) -> None:
        """Ensure one guild's scheduled night has a live, correct RSVP card.

        Shared by startup recovery and the periodic self-healing sweep — see
        ``ScheduleLifecycle``. A PENDING_CONFIRMATION night is intentionally
        skipped: nothing is published until the organizer confirms it.
        """
        if event.state is not EventState.SCHEDULED:
            return
        guild = ctx.get_guild(event.guild_id)
        if guild is None:
            return
        await self.ensure_rsvp_card(event, guild)
