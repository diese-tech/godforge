"""The scheduled-night `/party` subcommands: Discord adapter.

Feature module for the Issue #48 refactor. Owns `schedule`, `confirm`, `rsvp`,
`unrsvp`, `events`, `calendar`, and `open-scheduled` — previously defined
directly in ``bot.py``. Domain logic stays in ``utils/party_schedule.py``; this
module is strictly the Discord adapter.

These commands are registered onto the existing `/party` command group (they
are not their own top-level group, unlike `/scrim`), so ``register_schedule_
commands`` takes the group as a parameter instead of constructing one.

Opening a scheduled night hands off into the party lobby/ready-check surface,
so those pieces are injected via ``ScheduleCommandDeps`` rather than imported
directly, keeping this feature decoupled from the party feature's internals.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands

from utils.party import LobbyState
from utils.party_queue import QueueError
from utils.party_schedule import (
    EventState,
    Recurrence,
    ScheduleError,
    calendar_ics,
    convert_to_lobby,
    parse_local_start,
)


@dataclass
class ScheduleCommandDeps:
    """Collaborators injected into the scheduled-night command family."""

    schedule_repository: object
    party_repository: object
    party_queue_service: object
    ready_check_embed: Callable
    ready_check_view: Callable[[], discord.ui.View]
    lobby_card_embed: Callable
    lobby_card_view: Callable[[], discord.ui.View]
    # Issue #67: the public RSVP card service. Optional so existing tests
    # that construct ScheduleCommandDeps without it keep working; a deps
    # object missing this collaborator just skips card auto-publish/refresh.
    schedule_rsvp_service: object | None = None


def register_schedule_commands(
    group: app_commands.Group, deps: ScheduleCommandDeps
) -> None:
    """Register the scheduled-night subcommands onto the existing `/party` group.

    Individual commands are also attached as attributes on *group* (e.g.
    ``group.schedule``) for direct invocation in tests.
    """

    @group.command(
        name="schedule",
        description="Schedule a one-time or weekly SMITE custom night",
    )
    @app_commands.describe(
        title="A short name for the custom night",
        when="For example: Friday 8 PM or 2026-08-01 20:00",
        timezone_name="IANA timezone, for example America/New_York",
        recurrence="once or weekly",
        capacity="Number of active seats (2-20)",
        role_slots="Optional comma-separated roles",
        reminders="Comma-separated minutes before start, for example 60,15",
    )
    async def schedule(
        interaction: discord.Interaction,
        title: str,
        when: str,
        timezone_name: str,
        recurrence: str = "once",
        capacity: app_commands.Range[int, 2, 20] = 10,
        role_slots: str = "",
        reminders: str = "60,15",
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        try:
            starts_at = parse_local_start(when, timezone_name)
            recurrence_value = Recurrence(recurrence.strip().lower())
            reminder_values = tuple(
                int(value.strip()) for value in reminders.split(",") if value.strip()
            )
            if not reminder_values or any(
                value < 5 or value > 10080 for value in reminder_values
            ):
                raise ScheduleError("reminders must be 5-10080 minutes before start")
            event = deps.schedule_repository.create(
                guild_id=interaction.guild_id,
                organizer_id=interaction.user.id,
                title=title,
                starts_at=starts_at,
                timezone_name=timezone_name,
                recurrence=recurrence_value,
                capacity=capacity,
                role_slots=tuple(role_slots.split(",")),
                reminder_minutes=reminder_values,
                operation_id=f"discord:{interaction.id}:schedule",
            )
        except (ScheduleError, ValueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Confirm **{event.title}** at <t:{int(event.starts_at.timestamp())}:f>, "
            f"interpreted in `{event.timezone_name}`. "
            f"Run `/party confirm {event.event_id}` to publish it.",
            ephemeral=True,
        )

    @group.command(name="confirm", description="Confirm a scheduled night's timezone")
    async def confirm(interaction: discord.Interaction, event_id: str):
        try:
            event = deps.schedule_repository.confirm(event_id, interaction.user.id)
        except ScheduleError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # Issue #67: auto-publish the persistent RSVP card — no separate
        # Share step. Best-effort like every other public-card operation;
        # `/party rsvp` remains a working fallback either way.
        if deps.schedule_rsvp_service is not None and interaction.guild is not None:
            await deps.schedule_rsvp_service.ensure_rsvp_card(event, interaction.guild)
        await interaction.response.send_message(
            f"Scheduled **{event.title}** for <t:{int(event.starts_at.timestamp())}:f>. "
            f"RSVP on the card above, or with `/party rsvp {event.event_id}`."
        )

    @group.command(name="rsvp", description="Reserve a seat in a scheduled custom night")
    async def rsvp(interaction: discord.Interaction, event_id: str):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        scheduled = deps.schedule_repository.get(event_id)
        if scheduled is None or scheduled.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Scheduled night not found.", ephemeral=True
            )
            return
        profile = deps.party_repository.get_player_preferences(
            interaction.guild_id, interaction.user.id
        )
        try:
            event = deps.schedule_repository.rsvp(event_id, interaction.user.id, profile)
        except ScheduleError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # This command is a fallback surface for the RSVP card (Issue #67) —
        # a mutation through either one must refresh the same canonical card.
        if deps.schedule_rsvp_service is not None and interaction.guild is not None:
            await deps.schedule_rsvp_service.refresh_rsvp_card(event, interaction.guild)
        waitlisted = any(item.user_id == interaction.user.id for item in event.waitlist)
        await interaction.response.send_message(
            (
                f"You're on the waitlist at position "
                f"{next(i for i, item in enumerate(event.waitlist, 1) if item.user_id == interaction.user.id)}."
                if waitlisted
                else f"Seat reserved ({len(event.rsvps)}/{event.capacity})."
            ),
            ephemeral=True,
        )

    @group.command(name="unrsvp", description="Release a scheduled-night reservation")
    async def unrsvp(interaction: discord.Interaction, event_id: str):
        event = deps.schedule_repository.get(event_id)
        if event is None or event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Scheduled night not found.", ephemeral=True
            )
            return
        try:
            changed = deps.schedule_repository.cancel_rsvp(event_id, interaction.user.id)
        except ScheduleError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if deps.schedule_rsvp_service is not None and interaction.guild is not None:
            await deps.schedule_rsvp_service.refresh_rsvp_card(changed, interaction.guild)
        await interaction.response.send_message("Reservation released.", ephemeral=True)

    @group.command(name="events", description="List upcoming SMITE custom nights")
    async def events(interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        upcoming = deps.schedule_repository.list_upcoming(interaction.guild_id)
        if not upcoming:
            await interaction.response.send_message(
                "No custom nights are scheduled.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "\n".join(
                f"`{event.event_id}` **{event.title}** — "
                f"<t:{int(event.starts_at.timestamp())}:f> — "
                f"{len(event.rsvps)}/{event.capacity} RSVP"
                for event in upcoming
            ),
            ephemeral=True,
        )

    @group.command(name="calendar", description="Download a scheduled night as an ICS file")
    async def calendar(interaction: discord.Interaction, event_id: str):
        event = deps.schedule_repository.get(event_id)
        if event is None or event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Scheduled night not found.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            file=discord.File(
                io.BytesIO(calendar_ics(event)), filename=f"godforge-{event.event_id}.ics"
            ),
            ephemeral=True,
        )

    @group.command(
        name="open-scheduled",
        description="Convert a scheduled night into its live ready-check lobby",
    )
    async def open_scheduled(interaction: discord.Interaction, event_id: str):
        event = deps.schedule_repository.get(event_id)
        if event is None or event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Scheduled night not found.", ephemeral=True
            )
            return
        if event.organizer_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the organizer can open this lobby.", ephemeral=True
            )
            return
        try:
            lobby = await convert_to_lobby(
                event,
                deps.schedule_repository,
                deps.party_repository,
                deps.party_queue_service,
            )
        except (ScheduleError, ValueError, QueueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # Issue #67 point 5/6: a weekly rollover already carried the RSVP
        # card's delivery ref forward to the new occurrence in the same
        # transaction as mark_converted() — refresh *that* row's card so the
        # same Discord message now shows the next occurrence with a fresh
        # roster. A one-time night has no successor, so its own card gets
        # one final edit into its terminal "session started" state instead.
        if deps.schedule_rsvp_service is not None and interaction.guild is not None:
            successor = deps.schedule_repository.find_successor(event_id)
            target = successor or deps.schedule_repository.get(event_id)
            await deps.schedule_rsvp_service.refresh_rsvp_card(target, interaction.guild)
        queue = await deps.party_queue_service.get(lobby.lobby_id)
        if lobby.state is LobbyState.READY_CHECK and queue is not None:
            await interaction.response.send_message(
                content=" ".join(f"<@{member.user_id}>" for member in queue.active),
                embed=deps.ready_check_embed(lobby.lobby_id, queue),
                view=deps.ready_check_view(),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
        else:
            await interaction.response.send_message(
                content=f"**{event.title}** is now an ordinary GodForge lobby.",
                embed=deps.lobby_card_embed(lobby),
                view=deps.lobby_card_view(),
            )

    @group.command(
        name="session-refresh",
        description="Reconcile a scheduled night's public RSVP card without recreating it",
    )
    async def session_refresh(interaction: discord.Interaction, event_id: str):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        event = deps.schedule_repository.get(event_id)
        if event is None or event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Scheduled night not found.", ephemeral=True
            )
            return
        is_organizer = event.organizer_id == interaction.user.id
        can_manage = bool(getattr(interaction.user.guild_permissions, "manage_guild", False))
        if not (is_organizer or can_manage):
            await interaction.response.send_message(
                "Only the organizer or a server manager can refresh this card.",
                ephemeral=True,
            )
            return
        if event.state is not EventState.SCHEDULED:
            await interaction.response.send_message(
                "Confirm this scheduled night with `/party confirm` before it can have "
                "a public RSVP card.",
                ephemeral=True,
            )
            return
        if deps.schedule_rsvp_service is None:
            await interaction.response.send_message(
                "RSVP cards are not available.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, posted = await deps.schedule_rsvp_service.ensure_rsvp_card(event, interaction.guild)
        await interaction.followup.send(
            "Posted a new RSVP card." if posted else "RSVP card reconciled.",
            ephemeral=True,
        )

    group.schedule = schedule
    group.confirm = confirm
    group.rsvp = rsvp
    group.unrsvp = unrsvp
    group.events = events
    group.calendar = calendar
    group.open_scheduled = open_scheduled
    group.session_refresh = session_refresh
