"""Play panel, queue card, ready-check, and draft-launch orchestration.

Feature module for the Issue #48 refactor, hardened for the Issue #63
queue-first UX pass. This is the largest and most tightly-coupled surface
migrated out of ``bot.py``: the Play-panel button handler, queue
creation/join flows, the party-queue bootstrap helper, the queue-card button
handler, the ready-check button handler, and launching the draft engine
(local or activity backend) from a formed lobby.

Unlike the smaller command features extracted earlier, these handlers already
call each other directly and share a wide set of collaborators — that
coupling is inherent to the feature (a queue-card action can trigger a ready
check, which can trigger room provisioning; joining can fill a queue and
trigger a ready check; launching a draft reconciles the lobby and posts a
match-result card). ``PartyLobbyService`` makes that coupling explicit and
testable via one injected ``PartyLobbyDeps`` rather than scattered ``bot.py``
module globals, but does not pretend to fully decouple it — further
decomposition (e.g. splitting queue orchestration from draft launch) is future
work noted in the architecture roadmap.

Discord objects stay at this boundary; domain logic stays in
``utils/party.py``, ``utils/party_store.py``, ``utils/party_queue.py``,
``utils/party_draft.py``, and ``utils/team_formation.py``.

Issue #63 hardening rule that shapes most of this file: a Discord
interaction's own message (``interaction.message``) is *never* assumed to be
the canonical public queue card. The canonical card's location is whatever
is durably stored on ``lobby.delivery`` (``panel_channel_id`` /
``panel_message_id``); every mutation refreshes that stored message instead
of editing whatever message happened to trigger the interaction. The one
exception is a component click that lives directly on a persistent card
(e.g. clicking Ready on the ready-check message) — there, editing
``interaction.message`` *is* editing the right message, because Discord
guarantees the interaction's message is the exact component's own message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord

from utils.lifecycle import LifecycleContext
from utils.party import LobbyState, Participant, PlayerPreferences
from utils.party_draft import PartyDraftError
from utils.party_queue import QueueError, QueueStatus, ReadyStatus
from utils.team_formation import FormationMode


@dataclass
class PartyLobbyDeps:
    """Collaborators injected into `PartyLobbyService`.

    Callable fields exist either because the collaborator is itself
    feature-owned elsewhere (draft coordinator, match-result rendering) or
    because it must be looked up lazily (view constructors, per-guild room
    service, reserved match IDs) rather than captured once at wiring time.
    """

    party_repository: object
    party_queue_service: object
    party_draft_repository: object
    scrim_repository: object
    drafts: object
    draft_coordinator: object
    activity_client: object
    formatter: object
    settings_module: object
    reserve_match_id: Callable[[], str]
    match_room_service_for_guild: Callable
    channel_has_active: Callable[[int], str | None]
    save_active_draft: Callable[[int, str], None]
    ensure_match_history: Callable
    match_result_embed: Callable
    set_member_role: Callable
    log: object

    lobby_card_view: Callable[[], discord.ui.View]
    ready_check_view: Callable[[], discord.ui.View]
    role_preferences_view: Callable[[], discord.ui.View]
    create_lobby_view: Callable[..., discord.ui.View]
    join_preferences_view: Callable[..., discord.ui.View]
    match_result_view: Callable[[], discord.ui.View]
    match_formation_view: Callable[[], discord.ui.View] | None = None

    # Issue #63 additions. All follow the same "inject a small factory"
    # convention as the fields above so PartyLobbyService stays Discord-UI
    # agnostic and test-friendly.
    recruiting_card_view: Callable[[], discord.ui.View] | None = None
    queue_settings_view: Callable[[], discord.ui.View] | None = None
    transfer_organizer_view: Callable[..., discord.ui.View] | None = None
    queue_select_view: Callable[..., discord.ui.View] | None = None
    change_roles_view: Callable[..., discord.ui.View] | None = None
    already_in_queue_view: Callable[..., discord.ui.View] | None = None
    rename_modal: Callable[..., discord.ui.Modal] | None = None


class PartyLobbyService:
    def __init__(self, deps: PartyLobbyDeps) -> None:
        self.deps = deps
        # Serializes the "everyone just became ready" handoff per lobby so a
        # double-clicked or retried final Ready response can never provision
        # two rooms or send two match-ready pings for the same queue.
        self._handoff_locks: dict[str, asyncio.Lock] = {}
        self._handoff_locks_guard = asyncio.Lock()

    # -- Rendering --------------------------------------------------------

    def lobby_card_embed(
        self, lobby, guild: discord.Guild | None = None
    ) -> discord.Embed:
        owner_name = None
        if guild is not None:
            member = guild.get_member(lobby.organizer_id)
            owner_name = member.display_name if member else None
        participants = (
            ", ".join(f"<@{p.user_id}>" for p in lobby.participants) or "None"
        )
        embed = discord.Embed(
            title=lobby.display_title(owner_name),
            description=lobby.notes or "Reusable GodForge party queue",
            color=0x3498DB,
        )
        queue_line = f"`{lobby.queue_code}` · {lobby.mode.title()}"
        if lobby.region:
            queue_line += f" · {lobby.region.upper()}"
        embed.add_field(name="Queue", value=queue_line, inline=False)
        embed.add_field(name="Organizer", value=f"<@{lobby.organizer_id}>")
        embed.add_field(name="Format", value=lobby.format)
        embed.add_field(
            name="Roster",
            value=f"{len(lobby.participants)}/{lobby.capacity} · {participants}",
            inline=False,
        )
        embed.add_field(
            name="Rules",
            value=(
                f"Voice: {'required' if lobby.voice_required else 'optional'} · "
                f"Skill: {lobby.skill_band or 'open'}"
            ),
            inline=False,
        )
        if lobby.state is LobbyState.FORMING:
            value = f"Match forming · {len(lobby.participants)}/{lobby.capacity} ready"
            if lobby.delivery.match_channel_id:
                value += f" · Continue in <#{lobby.delivery.match_channel_id}>"
            embed.add_field(name="Status", value=value, inline=False)
        elif lobby.state is LobbyState.ACTIVE:
            value = "Match active · Draft and results are in the private room"
            if lobby.delivery.match_channel_id:
                value += f" · <#{lobby.delivery.match_channel_id}>"
            embed.add_field(name="Status", value=value, inline=False)
        elif lobby.state is LobbyState.EXPIRED:
            embed.add_field(
                name="Status", value="Queue expired from inactivity.", inline=False
            )
        elif lobby.state is LobbyState.CANCELLED:
            embed.add_field(name="Status", value="Queue cancelled.", inline=False)
        embed.set_footer(text=f"lobby_id={lobby.lobby_id}")
        return embed

    def ready_check_embed(self, lobby_id: str, queue) -> discord.Embed:
        ready_count = sum(status is ReadyStatus.READY for status in queue.ready.values())
        embed = discord.Embed(
            title="GodForge Ready Check",
            description=(
                f"{ready_count}/{len(queue.active)} ready. Choose Ready, "
                "Need 5 Minutes, or Drop."
            ),
            color=0xF1C40F,
        )
        # Issue #63: name exactly who is still blocking progress instead of
        # only showing a count, so the group knows who to nudge.
        outstanding = []
        for member in queue.active:
            status = queue.ready.get(member.user_id)
            if status is ReadyStatus.READY:
                continue
            label = f"<@{member.user_id}>"
            if status is ReadyStatus.NEED_5:
                label += " · needs 5 minutes"
            outstanding.append(label)
        if outstanding:
            embed.add_field(name="Waiting on", value="\n".join(outstanding), inline=False)
        if queue.ready_deadline:
            embed.add_field(
                name="Deadline",
                value=f"<t:{int(queue.ready_deadline.timestamp())}:R>",
            )
        embed.set_footer(text=f"lobby_id={lobby_id}")
        return embed

    def formation_card_embed(self, lobby) -> discord.Embed:
        embed = discord.Embed(
            title="GodForge Match Formation",
            description=(
                f"All {len(lobby.participants)} players are ready. "
                "The organizer can choose a deterministic team mode below."
            ),
            color=0x2ECC71,
        )
        embed.add_field(
            name="Private match workspace",
            value="Draft launch, team assignments, and results stay in this channel.",
            inline=False,
        )
        if lobby.state is LobbyState.FORMING:
            embed.add_field(
                name="Status",
                value=(
                    f"Match forming · {len(lobby.participants)}/{lobby.capacity} ready · "
                    "Private room created"
                ),
                inline=False,
            )
        elif lobby.state is LobbyState.ACTIVE:
            embed.add_field(
                name="Status",
                value="Match active · Draft and results are in the private room",
                inline=False,
            )
        embed.set_footer(text=f"lobby_id={lobby.lobby_id}")
        return embed

    async def ensure_match_formation_card(self, lobby, guild, rooms):
        """Create or refresh the durable private formation control message."""
        deps = self.deps
        channel = guild.get_channel(rooms.text_room_id)
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError("The private match text channel is unavailable.")
        delivery = lobby.delivery
        if (
            delivery.match_channel_id == rooms.text_room_id
            and delivery.formation_message_id
            and hasattr(channel, "fetch_message")
        ):
            try:
                message = await channel.fetch_message(delivery.formation_message_id)
                await message.edit(
                    content=f"<@{lobby.organizer_id}> choose how to form this match.",
                    embed=self.formation_card_embed(lobby),
                    view=(
                        deps.match_formation_view()
                        if deps.match_formation_view
                        else deps.lobby_card_view()
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False),
                )
                return lobby
            except (discord.NotFound, discord.HTTPException, AttributeError):
                pass
        message = await channel.send(
            content=f"<@{lobby.organizer_id}> choose how to form this match.",
            embed=self.formation_card_embed(lobby),
            view=(
                deps.match_formation_view()
                if deps.match_formation_view
                else deps.lobby_card_view()
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
        return deps.party_repository.set_delivery(
            lobby.guild_id,
            lobby.lobby_id,
            replace(
                lobby.delivery,
                match_channel_id=rooms.text_room_id,
                formation_message_id=message.id,
            ),
            operation_id=f"formation-delivery:{lobby.lobby_id}:{message.id}",
        )

    async def refresh_public_lobby_card(self, lobby, guild) -> None:
        """Best-effort refresh of the shared public status projection.

        This is the *only* mechanism that should update the canonical public
        queue card. It always resolves the card through the durable
        ``lobby.delivery`` reference — never through whichever Discord
        interaction happened to trigger the mutation — so an ephemeral join
        wizard, a settings panel, or a completely different queue's card can
        never be mistaken for this queue's public projection.

        Best-effort by design: a failed Discord edit here must never roll
        back the domain mutation that already succeeded.
        """
        delivery = lobby.delivery
        if not delivery.panel_channel_id or not delivery.panel_message_id:
            return
        channel = guild.get_channel(delivery.panel_channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(delivery.panel_message_id)
            recruiting = lobby.state in {LobbyState.OPEN, LobbyState.FULL}
            await message.edit(
                embed=self.lobby_card_embed(lobby, guild),
                view=self.deps.recruiting_card_view() if recruiting else None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            self.deps.log.exception(
                "Could not refresh public lobby card %s", lobby.lobby_id
            )

    async def ensure_public_lobby_card(self, lobby, guild) -> tuple[object, bool]:
        """Refresh the public card if it exists, else publish it fresh.

        Issue #63: queue creation must auto-publish (no more manual Share
        step), and a deleted/missing card must have a recoverable repost
        path. Both are the same operation — this is that operation. Returns
        ``(lobby, True)`` when a new message was posted, ``(lobby, False)``
        otherwise (already live and refreshed, or no Play channel configured
        yet).
        """
        deps = self.deps
        delivery = lobby.delivery
        if delivery.panel_channel_id and delivery.panel_message_id:
            channel = guild.get_channel(delivery.panel_channel_id)
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    await channel.fetch_message(delivery.panel_message_id)
                    await self.refresh_public_lobby_card(lobby, guild)
                    return lobby, False
                except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                        AttributeError):
                    pass  # Stored message is gone; fall through and repost.
        guild_settings = deps.settings_module.get_guild_settings(str(lobby.guild_id))
        channel_id = guild_settings["managed"].get("playChannelId")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None or not hasattr(channel, "send"):
            return lobby, False
        recruiting = lobby.state in {LobbyState.OPEN, LobbyState.FULL}
        message = await channel.send(
            embed=self.lobby_card_embed(lobby, guild),
            view=deps.recruiting_card_view() if recruiting else None,
        )
        updated = deps.party_repository.set_delivery(
            lobby.guild_id,
            lobby.lobby_id,
            replace(lobby.delivery, panel_channel_id=channel.id, panel_message_id=message.id),
            operation_id=f"public-lobby-delivery:{lobby.lobby_id}:{message.id}",
        )
        return updated, True

    async def ensure_ready_check_card(
        self, lobby, guild, queue, *, fallback_channel=None
    ):
        """Create or refresh the one durable ready-check message for a queue.

        Storing its channel/message IDs (mirroring how the formation card
        already works) means a retried "queue just filled" event, an
        organizer manually restarting a ready check, and restart recovery
        all edit the same message instead of posting duplicates.
        """
        deps = self.deps
        delivery = lobby.delivery
        content = " ".join(f"<@{member.user_id}>" for member in queue.active)
        if delivery.ready_channel_id and delivery.ready_message_id:
            channel = guild.get_channel(delivery.ready_channel_id)
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    message = await channel.fetch_message(delivery.ready_message_id)
                    await message.edit(
                        content=content,
                        embed=self.ready_check_embed(lobby.lobby_id, queue),
                        view=deps.ready_check_view(),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False),
                    )
                    return lobby
                except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                        AttributeError):
                    pass  # Stored message is gone; post a fresh one below.
        channel = None
        if delivery.panel_channel_id:
            channel = guild.get_channel(delivery.panel_channel_id)
        if channel is None or not hasattr(channel, "send"):
            channel = fallback_channel
        if channel is None or not hasattr(channel, "send"):
            return lobby
        message = await channel.send(
            content=content,
            embed=self.ready_check_embed(lobby.lobby_id, queue),
            view=deps.ready_check_view(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return lobby
        return deps.party_repository.set_delivery(
            lobby.guild_id,
            lobby.lobby_id,
            replace(lobby.delivery, ready_channel_id=channel_id, ready_message_id=message.id),
            operation_id=f"ready-delivery:{lobby.lobby_id}:{message.id}",
        )

    @staticmethod
    def lobby_id_from_interaction(interaction: discord.Interaction) -> str:
        """Read the lobby_id stamped in an embed footer.

        This is an identity lookup only — safe to use against *any* message
        that carries the footer (a public card, a ready-check card, or even
        an ephemeral settings panel we stamped ourselves), unlike editing
        ``interaction.message`` to represent live queue state.
        """
        embeds = getattr(interaction.message, "embeds", ())
        footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
        if not footer.startswith("lobby_id="):
            raise ValueError("This lobby card is missing its stable identity.")
        return footer.removeprefix("lobby_id=")

    # -- Queue bootstrap ----------------------------------------------------

    async def ensure_party_queue(self, lobby):
        deps = self.deps
        try:
            queue = await deps.party_queue_service.create(lobby.lobby_id, lobby.capacity)
        except QueueError:
            queue = await deps.party_queue_service.get(lobby.lobby_id)
        if queue is None:
            raise RuntimeError("party queue could not be initialized")
        if queue.capacity != lobby.capacity:
            queue, promoted_ids = await deps.party_queue_service.resize(
                lobby.lobby_id,
                lobby.capacity,
            )
            for promoted_id in promoted_ids:
                promoted = deps.party_repository.get_player_preferences(
                    lobby.guild_id,
                    promoted_id,
                )
                deps.party_repository.save_participant(
                    lobby.guild_id,
                    lobby.lobby_id,
                    Participant(
                        promoted_id,
                        primary_role=promoted.primary_role,
                        secondary_role=promoted.secondary_role,
                        fill=promoted.fill,
                        captain=promoted.captain,
                    ),
                    operation_id=(
                        f"capacity-sync:{lobby.lobby_id}:{lobby.version}:{promoted_id}"
                    ),
                )
        existing_ids = {member.user_id for member in (*queue.active, *queue.waitlist)}
        for participant in lobby.participants:
            if participant.user_id not in existing_ids:
                queue, _ = await deps.party_queue_service.join(
                    lobby.lobby_id,
                    participant.user_id,
                    participant.preferences,
                )
        return queue

    # -- Play panel ---------------------------------------------------------

    async def handle_play_panel_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        if interaction.guild is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        if action == "preferences":
            preferences = deps.party_repository.get_player_preferences(
                guild_id,
                interaction.user.id,
            )
            selected = ", ".join(preferences.roles) or "none yet"
            await interaction.response.send_message(
                f"Your role preferences: **{selected}**. Toggle them below.",
                view=deps.role_preferences_view(),
                ephemeral=True,
            )
            return
        active = [
            record.lobby
            for record in deps.party_repository.recover_active(guild_id)
            if record.lobby.state in {LobbyState.OPEN, LobbyState.FULL}
        ]
        if action == "browse":
            if not active:
                await interaction.response.send_message(
                    "No party queues are open yet.",
                    ephemeral=True,
                )
                return
            lobby = active[0]
            await interaction.response.send_message(
                embed=self.lobby_card_embed(lobby, interaction.guild),
                view=deps.lobby_card_view(),
                ephemeral=True,
            )
            for additional_lobby in active[1:]:
                await interaction.followup.send(
                    embed=self.lobby_card_embed(additional_lobby, interaction.guild),
                    view=deps.lobby_card_view(),
                    ephemeral=True,
                )
            return
        if action == "create":
            view = deps.create_lobby_view(self.handle_create_lobby_submission)
            await interaction.response.send_message(
                embed=view.summary_embed(), view=view, ephemeral=True
            )
            return
        if action == "queue":
            # Issue #63: this used to route to the first open lobby with
            # space, which is unsafe once a guild runs multiple queues at
            # once. Joinable queues are only ever auto-selected when there is
            # exactly one; otherwise the player must choose explicitly.
            joinable = [
                candidate
                for candidate in active
                if candidate.state is LobbyState.OPEN
                and len(candidate.participants) < candidate.capacity
            ]
            if not joinable:
                await interaction.response.send_message(
                    "No open queue has space right now. Start one instead.",
                    ephemeral=True,
                )
                return
            if len(joinable) == 1:
                await self.start_join_flow(interaction, joinable[0])
                return

            options: list[tuple[str, str]] = []
            for candidate in joinable:
                member = interaction.guild.get_member(candidate.organizer_id)
                name = candidate.display_title(member.display_name if member else None)
                options.append(
                    (
                        candidate.lobby_id,
                        f"{name} · {candidate.queue_code} · "
                        f"{len(candidate.participants)}/{candidate.capacity}",
                    )
                )

            async def select_handler(
                select_interaction: discord.Interaction, chosen_lobby_id: str
            ) -> None:
                chosen = deps.party_repository.get(guild_id, chosen_lobby_id)
                if chosen is None:
                    await select_interaction.response.send_message(
                        "That queue is no longer open.", ephemeral=True
                    )
                    return
                await self.start_join_flow(select_interaction, chosen)

            await interaction.response.send_message(
                "Multiple queues are open. Choose one to join.",
                view=deps.queue_select_view(select_handler, options),
                ephemeral=True,
            )

    async def handle_role_preference(
        self, interaction: discord.Interaction, role_key: str
    ) -> None:
        deps = self.deps
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        managed = deps.settings_module.get_guild_settings(str(guild_id))["managed"]
        stored_role_ids = managed["roleIds"]
        role_id = int(stored_role_ids.get(role_key) or 0)
        enabled = role_id not in {role.id for role in interaction.user.roles}
        await deps.set_member_role(
            interaction.guild,
            interaction.user,
            role_key,
            enabled,
            stored_role_ids,
        )
        profile = deps.party_repository.get_player_preferences(guild_id, interaction.user.id)
        roles = list(profile.roles)
        if role_key in {"solo", "jungle", "mid", "support", "adc"}:
            if enabled and role_key not in roles:
                roles.append(role_key)
            if not enabled and role_key in roles:
                roles.remove(role_key)
        saved = deps.party_repository.set_player_preferences(
            guild_id,
            interaction.user.id,
            PlayerPreferences(
                roles[0] if roles else None,
                roles[1] if len(roles) > 1 else None,
                profile.fill,
                enabled if role_key == "captain" else profile.captain,
            ),
        )
        await interaction.response.send_message(
            f"{role_key.title()} {'added' if enabled else 'removed'}. "
            f"Preferences: {', '.join(saved.roles) or 'none'}.",
            ephemeral=True,
        )

    async def handle_create_lobby_submission(
        self, interaction: discord.Interaction, payload: dict[str, object]
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        capacity = int(payload["party_size"])
        if capacity not in {2, 4, 6, 8, 10}:
            await interaction.response.send_message(
                "Lobby capacity must be 2, 4, 6, 8, or 10 players.",
                ephemeral=True,
            )
            return
        test_mode = deps.settings_module.get_guild_settings(str(guild_id))["managed"].get(
            "testMode",
            False,
        )
        lobby = deps.party_repository.create(
            guild_id=guild_id,
            organizer_id=interaction.user.id,
            capacity=capacity,
            # Issue #63: recruiting queues expire from inactivity, not from a
            # single fixed timer — this is the starting clock, extended by
            # touch_recruiting_activity() on every meaningful action.
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=10 if test_mode else 60),
            operation_id=f"discord:{interaction.id}:create",
            mode=str(payload["mode"]),
            region=str(payload["region"]),
            format=str(payload["format"]),
            voice_required=bool(payload["voice_required"]),
            skill_band=str(payload.get("skill_band") or ""),
            notes=str(payload.get("notes") or ""),
            display_name=str(payload.get("queue_name") or ""),
        )
        profile = deps.party_repository.get_player_preferences(guild_id, interaction.user.id)
        lobby = deps.party_repository.save_participant(
            guild_id,
            lobby.lobby_id,
            Participant(
                interaction.user.id,
                primary_role=profile.primary_role,
                secondary_role=profile.secondary_role,
                fill=profile.fill,
                captain=profile.captain,
            ),
            operation_id=f"discord:{interaction.id}:organizer",
        )
        await self.ensure_party_queue(lobby)

        # Issue #63: creation now auto-publishes the canonical public card —
        # a normal organizer never has to click Share/Repost afterwards.
        posted = False
        if interaction.guild is not None:
            lobby, posted = await self.ensure_public_lobby_card(lobby, interaction.guild)
        if posted:
            confirmation = f"**{lobby.display_title()}** (`{lobby.queue_code}`) is live in the Play channel."
        elif lobby.delivery.panel_channel_id:
            confirmation = f"**{lobby.display_title()}** (`{lobby.queue_code}`) is ready."
        else:
            confirmation = (
                f"**{lobby.display_title()}** (`{lobby.queue_code}`) was created, but no "
                "GodForge Play channel is configured yet. Run `/party setup`, then use "
                "Queue Settings → Repost Queue."
            )
        await interaction.response.send_message(
            content=confirmation,
            embed=self.lobby_card_embed(lobby, interaction.guild),
            view=deps.recruiting_card_view(),
            ephemeral=True,
        )

    async def start_join_flow(self, interaction: discord.Interaction, lobby) -> None:
        """Route a Join Queue click to the fastest valid path.

        Issue #63: a returning player with a saved primary role joins
        immediately using those saved preferences; a first-time player (or
        one without enough saved preference) sees the short role wizard
        instead. Neither path ever asks for captain willingness here.
        """
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        if lobby.participant(interaction.user.id) is not None:
            await interaction.response.send_message(
                f"You're already in **{lobby.display_title()}**.", ephemeral=True
            )
            return
        existing_other = deps.party_repository.find_active_lobby_for_player(
            guild_id, interaction.user.id, exclude_lobby_id=lobby.lobby_id
        )
        if existing_other is not None:
            await self._reject_double_booking(interaction, existing_other)
            return
        saved = deps.party_repository.get_player_preferences(guild_id, interaction.user.id)
        if saved.primary_role:
            payload = {
                "primary_role": saved.primary_role,
                "secondary_role": saved.secondary_role or "",
                "fill": saved.fill,
                "captain": saved.captain,
            }
            await self.join_lobby_from_preferences(
                interaction, lobby.lobby_id, payload, change_roles_prompt=True,
            )
            return

        async def join_handler(join_interaction, join_payload):
            await self.join_lobby_from_preferences(join_interaction, lobby.lobby_id, join_payload)

        await interaction.response.send_message(
            f"Choose your role preferences for **{lobby.display_title()}**.",
            view=deps.join_preferences_view(join_handler),
            ephemeral=True,
        )

    async def _open_change_roles(self, interaction: discord.Interaction, lobby_id: str) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        lobby = deps.party_repository.get(guild_id, lobby_id) if guild_id else None
        title = lobby.display_title() if lobby else "this queue"

        async def join_handler(join_interaction, join_payload):
            await self.join_lobby_from_preferences(join_interaction, lobby_id, join_payload)

        await interaction.response.send_message(
            f"Update your role preferences for **{title}**.",
            view=deps.join_preferences_view(join_handler),
            ephemeral=True,
        )

    async def _reject_double_booking(
        self, interaction: discord.Interaction, existing_lobby
    ) -> None:
        """Issue #63 locked-in rule: one active queue per player per guild."""
        deps = self.deps

        async def leave_other(leave_interaction: discord.Interaction) -> None:
            changed = await self._process_leave(
                leave_interaction, existing_lobby.lobby_id, leave_interaction.user.id
            )
            if changed is None:
                await leave_interaction.response.send_message(
                    "That queue is no longer active.", ephemeral=True
                )
                return
            await leave_interaction.response.send_message(
                f"Left **{existing_lobby.display_title()}**. Click Join Queue again to switch.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"You're already in **{existing_lobby.display_title()}** "
            f"(`{existing_lobby.queue_code}`). Leave it first if you want to join a "
            "different queue.",
            view=deps.already_in_queue_view(leave_other),
            ephemeral=True,
        )

    async def join_lobby_from_preferences(
        self,
        interaction: discord.Interaction,
        lobby_id: str,
        payload: dict[str, object],
        *,
        change_roles_prompt: bool = False,
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        lobby = deps.party_repository.get(guild_id, lobby_id)
        if lobby is None:
            await interaction.response.send_message(
                "That queue no longer exists. Use Find a Queue to pick an active one.",
                ephemeral=True,
            )
            return
        # Issue #63 locked-in edge case: a player may only be active in one
        # queue per guild at a time. Checked before any state changes.
        existing_other = deps.party_repository.find_active_lobby_for_player(
            guild_id, interaction.user.id, exclude_lobby_id=lobby_id
        )
        if existing_other is not None:
            await self._reject_double_booking(interaction, existing_other)
            return
        current_profile = deps.party_repository.get_player_preferences(
            guild_id, interaction.user.id
        )
        profile = PlayerPreferences(
            str(payload["primary_role"]),
            str(payload.get("secondary_role") or "") or None,
            bool(payload["fill"]),
            # Captain willingness is no longer collected by the join wizard;
            # whatever was already saved in My Preferences carries over.
            bool(payload["captain"]) if "captain" in payload else current_profile.captain,
        )
        deps.party_repository.set_player_preferences(
            guild_id,
            interaction.user.id,
            profile,
        )
        await self.ensure_party_queue(lobby)
        try:
            queue, destination = await deps.party_queue_service.join(
                lobby_id,
                interaction.user.id,
                profile.roles,
            )
        except QueueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if destination in {"active", "unchanged"}:
            changed = deps.party_repository.save_participant(
                guild_id,
                lobby_id,
                Participant(
                    interaction.user.id,
                    primary_role=profile.primary_role,
                    secondary_role=profile.secondary_role,
                    fill=profile.fill,
                    captain=profile.captain,
                ),
                operation_id=f"discord:{interaction.id}:join",
            )
        else:
            changed = lobby
        if changed.state in {LobbyState.OPEN, LobbyState.FULL}:
            changed = deps.party_repository.touch_recruiting_activity(
                guild_id, lobby_id, operation_id=f"discord:{interaction.id}:touch"
            )

        # The click may have come from an ephemeral join wizard (or the fast
        # saved-preference path) rather than the public queue card itself.
        # Always refresh the saved public projection via its stored delivery
        # reference instead of assuming this interaction's own message is
        # that card.
        if interaction.guild is not None:
            await self.refresh_public_lobby_card(changed, interaction.guild)

        if destination == "waitlist":
            ack = (
                f"**{changed.display_title()}** is full; added to the waitlist at "
                f"position {len(queue.waitlist)}."
            )
        else:
            role_summary = "/".join(profile.roles) or "Fill"
            ack = (
                f"Joined **{changed.display_title()}** as {role_summary} · "
                f"{len(changed.participants)}/{changed.capacity}."
            )
        ack_view = None
        if change_roles_prompt:
            async def change_roles_handler(cr_interaction: discord.Interaction) -> None:
                await self._open_change_roles(cr_interaction, lobby_id)

            ack_view = deps.change_roles_view(change_roles_handler)
        await interaction.response.send_message(ack, view=ack_view, ephemeral=True)

        if len(queue.active) == queue.capacity and queue.status is QueueStatus.OPEN:
            queue = await deps.party_queue_service.start_ready_check(lobby_id)
            if changed.state is LobbyState.OPEN:
                changed = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.FULL,
                    operation_id=f"discord:{interaction.id}:full",
                )
            if changed.state is LobbyState.FULL:
                changed = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.READY_CHECK,
                    operation_id=f"discord:{interaction.id}:ready-check",
                )
            if interaction.guild is not None:
                await self.refresh_public_lobby_card(changed, interaction.guild)
                await self.ensure_ready_check_card(
                    changed, interaction.guild, queue, fallback_channel=interaction.channel,
                )

    # -- Leave / organizer succession ----------------------------------------

    async def _apply_organizer_succession(
        self, guild_id: int, lobby_id: str, changed, departed_user_id: int,
        *, operation_prefix: str, actor_id: int,
    ):
        """Issue #63 locked-in edge case: a queue is never left organizer-less.

        If the departing player was the organizer, ownership transfers
        automatically to the longest-tenured remaining participant; if no
        one remains, the queue is cancelled. Otherwise this is a no-op.
        """
        deps = self.deps
        if changed.organizer_id != departed_user_id or changed.is_terminal:
            return changed
        remaining = sorted(changed.participants, key=lambda participant: participant.joined_at)
        if remaining:
            return deps.party_repository.transfer_organizer(
                guild_id, lobby_id, remaining[0].user_id,
                operation_id=f"{operation_prefix}:auto-transfer",
                actor_id=actor_id,
            )
        return deps.party_repository.transition(
            guild_id, lobby_id, LobbyState.CANCELLED,
            operation_id=f"{operation_prefix}:auto-cancel-empty",
            actor_id=actor_id,
            reason="organizer left an empty queue",
        )

    async def _process_leave(
        self, interaction: discord.Interaction, lobby_id: str, user_id: int
    ):
        deps = self.deps
        guild_id = interaction.guild_id
        lobby = deps.party_repository.get(guild_id, lobby_id)
        if lobby is None:
            return None
        await self.ensure_party_queue(lobby)
        queue, promoted_id = await deps.party_queue_service.leave(lobby_id, user_id)
        changed = deps.party_repository.remove_participant(
            guild_id, lobby_id, user_id,
            operation_id=f"discord:{interaction.id}:leave",
            actor_id=user_id,
        )
        if promoted_id is not None:
            promoted = deps.party_repository.get_player_preferences(guild_id, promoted_id)
            changed = deps.party_repository.save_participant(
                guild_id, lobby_id,
                Participant(
                    promoted_id,
                    primary_role=promoted.primary_role,
                    secondary_role=promoted.secondary_role,
                    fill=promoted.fill,
                    captain=promoted.captain,
                ),
                operation_id=f"discord:{interaction.id}:promote:{promoted_id}",
            )
        changed = await self._apply_organizer_succession(
            guild_id, lobby_id, changed, user_id,
            operation_prefix=f"discord:{interaction.id}", actor_id=user_id,
        )
        if changed.state in {LobbyState.OPEN, LobbyState.FULL}:
            changed = deps.party_repository.touch_recruiting_activity(
                guild_id, lobby_id, operation_id=f"discord:{interaction.id}:touch"
            )
        if interaction.guild is not None:
            await self.refresh_public_lobby_card(changed, interaction.guild)
        return changed

    # -- Lobby card -----------------------------------------------------------

    async def handle_lobby_card_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        lobby_id = self.lobby_id_from_interaction(interaction)
        active_ids = {
            record.lobby.lobby_id
            for record in deps.party_repository.recover_active(guild_id)
        }
        lobby = deps.party_repository.get(guild_id, lobby_id)
        if lobby is None or lobby_id not in active_ids:
            await interaction.response.send_message(
                "That queue is no longer active.",
                ephemeral=True,
            )
            return
        if action == "join":
            await self.start_join_flow(interaction, lobby)
            return
        if action == "leave":
            changed = await self._process_leave(interaction, lobby_id, interaction.user.id)
            await interaction.response.send_message("Left the queue.", ephemeral=True)
            return
        if action == "queue_settings":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can open Queue Settings.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                embed=self.lobby_card_embed(lobby, interaction.guild),
                view=deps.queue_settings_view(),
                ephemeral=True,
            )
            return
        if action == "rename":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can rename this queue.", ephemeral=True
                )
                return

            async def submit_rename(modal_interaction: discord.Interaction, new_name: str) -> None:
                try:
                    renamed = deps.party_repository.rename(
                        guild_id, lobby_id, new_name,
                        operation_id=f"discord:{modal_interaction.id}:rename",
                        actor_id=modal_interaction.user.id,
                    )
                except (PermissionError, ValueError) as exc:
                    await modal_interaction.response.send_message(str(exc), ephemeral=True)
                    return
                if modal_interaction.guild is not None:
                    await self.refresh_public_lobby_card(renamed, modal_interaction.guild)
                await modal_interaction.response.send_message(
                    f"Queue renamed to **{renamed.display_title()}**.", ephemeral=True
                )

            await interaction.response.send_modal(
                deps.rename_modal(submit_rename, lobby.display_name)
            )
            return
        if action == "repost":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can repost this queue.", ephemeral=True
                )
                return
            changed, posted_new = await self.ensure_public_lobby_card(lobby, interaction.guild)
            if posted_new:
                await interaction.response.send_message(
                    "Queue reposted to the Play channel.", ephemeral=True
                )
            elif changed.delivery.panel_channel_id:
                await interaction.response.send_message(
                    "The queue card is already live; refreshed it.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "No GodForge Play channel is configured yet. Run `/party setup` first.",
                    ephemeral=True,
                )
            return
        if action == "transfer_organizer":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can transfer ownership.", ephemeral=True
                )
                return
            candidates = [p for p in lobby.participants if p.user_id != lobby.organizer_id]
            if not candidates:
                await interaction.response.send_message(
                    "No other players are in this queue yet.", ephemeral=True
                )
                return
            options = []
            for participant in candidates:
                member = (
                    interaction.guild.get_member(participant.user_id)
                    if interaction.guild else None
                )
                options.append(
                    (participant.user_id, member.display_name if member else
                     f"Player {participant.user_id}")
                )

            async def transfer_handler(
                select_interaction: discord.Interaction, new_organizer_id: int
            ) -> None:
                try:
                    changed = deps.party_repository.transfer_organizer(
                        guild_id, lobby_id, new_organizer_id,
                        operation_id=f"discord:{select_interaction.id}:transfer",
                        actor_id=select_interaction.user.id,
                    )
                except (PermissionError, ValueError) as exc:
                    await select_interaction.response.send_message(str(exc), ephemeral=True)
                    return
                if select_interaction.guild is not None:
                    await self.refresh_public_lobby_card(changed, select_interaction.guild)
                await select_interaction.response.send_message(
                    f"<@{new_organizer_id}> is now the organizer.", ephemeral=True,
                )

            await interaction.response.send_message(
                "Choose the new organizer.",
                view=deps.transfer_organizer_view(transfer_handler, options),
                ephemeral=True,
            )
            return
        if action == "ready_check":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can start a ready check.",
                    ephemeral=True,
                )
                return
            if lobby.state not in {LobbyState.OPEN, LobbyState.FULL}:
                await interaction.response.send_message(
                    "This queue is not currently recruiting.",
                    ephemeral=True,
                )
                return
            queue = await self.ensure_party_queue(lobby)
            if queue.status is not QueueStatus.OPEN:
                await interaction.response.send_message(
                    "A ready check is already active or this queue is closed.",
                    ephemeral=True,
                )
                return
            queue = await deps.party_queue_service.start_ready_check(lobby_id)
            if lobby.state is LobbyState.OPEN and len(queue.active) == lobby.capacity:
                lobby = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.FULL,
                    operation_id=f"discord:{interaction.id}:full",
                )
            if lobby.state in {LobbyState.FULL, LobbyState.OPEN}:
                lobby = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.READY_CHECK,
                    operation_id=f"discord:{interaction.id}:ready-check",
                )
            await self.refresh_public_lobby_card(lobby, interaction.guild)
            await self.ensure_ready_check_card(
                lobby, interaction.guild, queue, fallback_channel=interaction.channel,
            )
            await interaction.response.send_message(
                "Ready check started.", ephemeral=True,
            )
            return
        formation_actions = {
            # Keep the v2.3-rc.1 custom ID recoverable for cards posted before this
            # deployment; refreshed cards expose the three explicit modes.
            "launch_draft": FormationMode.ROLE_FIT,
            "teams_role_fit": FormationMode.ROLE_FIT,
            "teams_balanced": FormationMode.BALANCED,
            "teams_captains": FormationMode.CAPTAINS,
        }
        if action in {"room_lock", "room_unlock", "room_close"}:
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can control these rooms.", ephemeral=True
                )
                return
            service = deps.match_room_service_for_guild(interaction.guild)
            try:
                if action == "room_lock":
                    await service.lock(lobby.lobby_id, actor_id=interaction.user.id)
                    label = "locked"
                elif action == "room_unlock":
                    await service.unlock(lobby.lobby_id, actor_id=interaction.user.id)
                    label = "unlocked"
                else:
                    changed = deps.party_repository.transition(
                        guild_id,
                        lobby_id,
                        LobbyState.CANCELLED,
                        operation_id=f"discord:{interaction.id}:room-close",
                        actor_id=interaction.user.id,
                    )
                    await self.refresh_public_lobby_card(changed, interaction.guild)
                    await service.close(
                        lobby.lobby_id,
                        actor_id=interaction.user.id,
                        reason="organizer closed rooms",
                        outcome="cancelled",
                    )
                    label = "closed"
            except (LookupError, PermissionError, ValueError, RuntimeError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Private match rooms {label}.", ephemeral=True
            )
            return
        if action == "move_teams_voice":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can move formed teams.", ephemeral=True
                )
                return
            if not lobby.voice_required:
                await interaction.response.send_message(
                    "This lobby was created without team voice rooms.", ephemeral=True
                )
                return
            launch = deps.party_draft_repository.get(lobby.lobby_id)
            if launch is None or launch.status != "active":
                await interaction.response.send_message(
                    "Form and launch the teams before moving players to voice.",
                    ephemeral=True,
                )
                return
            assignments = {
                **{user_id: 1 for user_id in launch.blue.participant_ids},
                **{user_id: 2 for user_id in launch.red.participant_ids},
            }
            failures = await deps.match_room_service_for_guild(
                interaction.guild
            ).move_players(
                lobby.lobby_id,
                actor_id=interaction.user.id,
                lobby_voice_id=None,
                team_assignments=assignments,
            )
            moved = len(assignments) - len(failures)
            message = f"{moved} moved to their saved team voice rooms."
            if failures:
                message += "\n" + "\n".join(
                    f"<@{user_id}>: {error}"
                    for user_id, error in sorted(failures.items())
                )
            await interaction.response.send_message(message, ephemeral=True)
            return
        if action in formation_actions:
            await self.launch_party_draft(
                interaction, lobby, formation_mode=formation_actions[action]
            )
            return
        if action in {"edit", "cancel"} and interaction.user.id != lobby.organizer_id:
            await interaction.response.send_message(
                "Only the organizer can change or cancel this lobby.",
                ephemeral=True,
            )
            return
        if action == "cancel":
            changed = deps.party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.CANCELLED,
                operation_id=f"discord:{interaction.id}:cancel",
                actor_id=interaction.user.id,
            )
            await self.refresh_public_lobby_card(changed, interaction.guild)
            await interaction.response.send_message("Queue cancelled.", ephemeral=True)
            return
        if action == "edit":
            async def edit_handler(edit_interaction, payload):
                changed = deps.party_repository.update_metadata(
                    guild_id,
                    lobby_id,
                    operation_id=f"discord:{edit_interaction.id}:edit",
                    actor_id=edit_interaction.user.id,
                    mode=str(payload["mode"]),
                    region=str(payload["region"]),
                    format=str(payload["format"]),
                    capacity=int(payload["party_size"]),
                    voice_required=bool(payload["voice_required"]),
                    skill_band=str(payload.get("skill_band") or ""),
                    notes=str(payload.get("notes") or ""),
                )
                if changed.state in {LobbyState.OPEN, LobbyState.FULL}:
                    changed = deps.party_repository.touch_recruiting_activity(
                        guild_id, lobby_id, operation_id=f"discord:{edit_interaction.id}:touch"
                    )
                if edit_interaction.guild is not None:
                    await self.refresh_public_lobby_card(changed, edit_interaction.guild)
                await edit_interaction.response.send_message(
                    "Queue details updated.", ephemeral=True,
                )

            initial = {
                "mode": lobby.mode,
                "region": lobby.region,
                "format": lobby.format,
                "party_size": lobby.capacity,
                "voice_required": lobby.voice_required,
                "skill_band": lobby.skill_band or "open",
                "notes": lobby.notes,
            }
            view = deps.create_lobby_view(edit_handler, initial=initial)
            await interaction.response.send_message(
                embed=view.summary_embed(), view=view, ephemeral=True
            )
            return
        if action == "share":
            # Legacy custom_id kept recoverable for cards posted before Issue
            # #63; behaves the same as the new "repost" action.
            await self.handle_lobby_card_action(interaction, "repost")

    # -- Draft launch ---------------------------------------------------------

    def reconcile_active_party_draft(self, lobby, launch, actor_id: int) -> None:
        """Idempotently project a successfully started draft onto its party lobby."""
        deps = self.deps
        current = deps.party_repository.get(lobby.guild_id, lobby.lobby_id)
        if current is None or current.state is LobbyState.ACTIVE:
            return
        if current.state is not LobbyState.FORMING:
            raise PartyDraftError(
                f"active draft cannot reconcile lobby from {current.state}"
            )
        deps.party_repository.transition(
            lobby.guild_id,
            lobby.lobby_id,
            LobbyState.ACTIVE,
            operation_id=f"party-draft-active:{lobby.lobby_id}:{launch.match_id}",
            actor_id=actor_id,
        )

    async def launch_party_draft(
        self,
        interaction: discord.Interaction,
        lobby,
        *,
        formation_mode: FormationMode = FormationMode.ROLE_FIT,
    ) -> None:
        """Confirm deterministic teams and launch the existing draft engine."""
        deps = self.deps
        if interaction.user.id != lobby.organizer_id:
            await interaction.response.send_message(
                "Only the organizer can confirm teams and launch the draft.",
                ephemeral=True,
            )
            return
        existing_launch = deps.party_draft_repository.get(lobby.lobby_id)
        if existing_launch and existing_launch.status == "active":
            deps.ensure_match_history(lobby, existing_launch)
            self.reconcile_active_party_draft(lobby, existing_launch, interaction.user.id)
            await interaction.response.send_message(
                f"Draft `{existing_launch.match_id}` is already active.",
                ephemeral=True,
            )
            return
        if lobby.state is not LobbyState.FORMING:
            await interaction.response.send_message(
                "Finish the ready check before launching a draft.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "The private match channel is unavailable in this server.",
                ephemeral=True,
            )
            return
        room_service = deps.match_room_service_for_guild(interaction.guild)
        rooms = await room_service.get(lobby.lobby_id)
        channel = (
            interaction.guild.get_channel(rooms.text_room_id)
            if rooms is not None
            else None
        )
        if channel is None or not hasattr(channel, "send"):
            await interaction.response.send_message(
                "The private match room is missing. Run `/party room` after GodForge "
                "reconciles the room, then retry formation.",
                ephemeral=True,
            )
            return
        if deps.channel_has_active(channel.id):
            await interaction.response.send_message(
                "This channel already has an active session or draft.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        launch = None
        draft_started = False
        try:
            scrim = deps.scrim_repository.get_challenge_by_lobby(
                lobby.guild_id, lobby.lobby_id
            )
            launch, should_start = deps.party_draft_repository.begin(
                lobby,
                operation_id=f"discord:{interaction.id}:party-draft",
                channel_id=channel.id,
                match_id_factory=deps.reserve_match_id,
                formation_mode=formation_mode,
                fixed_teams=(
                    deps.scrim_repository.fixed_draft_teams(scrim) if scrim else None
                ),
            )
            if not should_start:
                if launch.status == "active":
                    self.reconcile_active_party_draft(lobby, launch, interaction.user.id)
                    message = f"Draft `{launch.match_id}` is already active."
                else:
                    message = "That draft launch is already in progress."
                await interaction.followup.send(message, ephemeral=True)
                return

            blue_member = interaction.guild.get_member(launch.blue.captain_id)
            red_member = interaction.guild.get_member(launch.red.captain_id)
            if blue_member is None or red_member is None:
                raise PartyDraftError("a selected captain is no longer in this server")

            if deps.activity_client.enabled:
                result = await deps.activity_client.post(
                    "/api/draft/start",
                    {
                        "blueCaptainId": str(blue_member.id),
                        "blueCaptainName": blue_member.display_name,
                        "redCaptainId": str(red_member.id),
                        "redCaptainName": red_member.display_name,
                        "guildId": str(lobby.guild_id),
                        "channelId": str(channel.id),
                        "godforgeMatchId": launch.match_id,
                        "party": launch.snapshot,
                    },
                )
                if not result or not result.get("matchId"):
                    raise PartyDraftError("the Activity backend did not start the draft")
                activity_id = result["matchId"]
                # Register the activity draft through the coordinator, which owns
                # the in-memory draft state and the WS listener (Issue #48).
                deps.draft_coordinator.match_ids[channel.id] = activity_id
                deps.draft_coordinator.match_channels[activity_id] = channel.id
                if result.get("state"):
                    deps.draft_coordinator.snapshots[channel.id] = result["state"]
                    sent = await channel.send(
                        f"\U0001f3ae Draft `{activity_id}` started — open the Activity and "
                        "enter this ID to join",
                        embed=deps.formatter.format_board_from_snapshot(result["state"]),
                    )
                    deps.draft_coordinator.board_message_ids[channel.id] = sent.id
                deps.draft_coordinator.start_ws(activity_id, channel.id)
            else:
                draft = deps.drafts.start(
                    channel.id,
                    blue_member.id,
                    blue_member.display_name,
                    red_member.id,
                    red_member.display_name,
                    lobby.guild_id,
                    interaction.guild.name,
                    getattr(channel, "name", "party-draft"),
                    match_id=launch.match_id,
                    party_context=launch.snapshot,
                )
                if draft is None:
                    raise PartyDraftError("this channel already has an active draft")
                deps.save_active_draft(channel.id, draft.draft_id)

            launch = deps.party_draft_repository.mark_active(lobby.lobby_id)
            draft_started = True
            match_record = deps.ensure_match_history(lobby, launch)
            self.reconcile_active_party_draft(lobby, launch, interaction.user.id)
            active_lobby = deps.party_repository.get(lobby.guild_id, lobby.lobby_id)
            if active_lobby is not None:
                await self.ensure_match_formation_card(
                    active_lobby, interaction.guild, rooms
                )
                await self.refresh_public_lobby_card(active_lobby, interaction.guild)
            formation = launch.snapshot.get("formation") or {}
            assignment_lines = []
            for side, emoji in (("blue", "\U0001f535"), ("red", "\U0001f534")):
                assignments = formation.get(side) or []
                assignment_lines.append(
                    f"{emoji} "
                    + " · ".join(
                        f"**{str(item['role']).title()}** <@{item['user_id']}>"
                        for item in assignments
                    )
                )
            formation_summary = ""
            if formation:
                label = str(formation.get("mode", formation_mode.value))
                formation_summary = (
                    f"\n\n**{label.replace('_', ' ').title()}**\n"
                    f"{formation.get('explanation', '')}\n"
                    + "\n".join(assignment_lines)
                )
            await channel.send(
                f"⚔️ Draft `{launch.match_id}` launched from lobby `{lobby.lobby_id[:8]}`.\n"
                f"\U0001f535 Captain <@{launch.blue.captain_id}>: "
                + " ".join(f"<@{user_id}>" for user_id in launch.blue.participant_ids)
                + f"\n\U0001f534 Captain <@{launch.red.captain_id}>: "
                + " ".join(f"<@{user_id}>" for user_id in launch.red.participant_ids)
                + formation_summary,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
            await channel.send(
                embed=deps.match_result_embed(match_record),
                view=deps.match_result_view(),
            )
            await interaction.followup.send(
                f"Draft `{launch.match_id}` started. Teams and lobby rules are retained.",
                ephemeral=True,
            )
        except Exception as exc:
            if launch is not None and not draft_started:
                deps.party_draft_repository.mark_failed(lobby.lobby_id, str(exc))
            deps.log.exception(
                "Party draft launch failed for lobby %s", lobby.lobby_id
            )
            if draft_started:
                await interaction.followup.send(
                    f"Draft `{launch.match_id}` started, but GodForge could not refresh "
                    "the lobby card. Press the same formation action again to reconcile "
                    "it safely.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "Draft launch failed. The lobby is still ready to retry with "
                    "the same private formation action.",
                    ephemeral=True,
                )

    # -- Ready check ------------------------------------------------------------

    async def _handoff_lock_for(self, lobby_id: str) -> asyncio.Lock:
        async with self._handoff_locks_guard:
            return self._handoff_locks.setdefault(lobby_id, asyncio.Lock())

    async def _send_match_ready_handoff(self, lobby, guild, rooms) -> None:
        """Send the one-time roster ping with a clickable channel mention.

        Issue #63: this must happen exactly once per successful handoff, no
        matter how many times the final Ready response (or a recovery pass)
        replays this lobby. Gating on the durable
        ``delivery.match_ready_notified`` flag — rather than only on the
        state transition — means a retry after a *failed* send still gets a
        chance to actually deliver the ping.
        """
        deps = self.deps
        if lobby.delivery.match_ready_notified:
            return
        channel_id = lobby.delivery.ready_channel_id or lobby.delivery.panel_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None or not hasattr(channel, "send"):
            channel = guild.get_channel(rooms.text_room_id)
        if channel is None or not hasattr(channel, "send"):
            return
        mentions = " ".join(f"<@{participant.user_id}>" for participant in lobby.participants)
        try:
            await channel.send(
                content=(
                    f"{mentions}\n\nMatch ready! Continue in <#{rooms.text_room_id}>."
                ),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            deps.log.exception(
                "Match-ready roster ping failed for lobby %s", lobby.lobby_id
            )
            return
        deps.party_repository.set_delivery(
            lobby.guild_id,
            lobby.lobby_id,
            replace(lobby.delivery, match_ready_notified=True),
            operation_id=f"match-ready-notified:{lobby.lobby_id}",
        )

    async def _complete_ready_check(
        self, interaction: discord.Interaction, guild_id: int, lobby_id: str, queue
    ) -> None:
        """Advance a fully-ready queue into forming, exactly once.

        Callers must hold the per-lobby handoff lock before calling this —
        see ``handle_ready_check_action``.
        """
        deps = self.deps
        room_failure = None
        lobby = deps.party_repository.get(guild_id, lobby_id)
        rooms = None
        if lobby and interaction.guild and lobby.state is LobbyState.READY_CHECK:
            try:
                rooms = await deps.match_room_service_for_guild(
                    interaction.guild
                ).provision(
                    guild_id=guild_id,
                    lobby_id=lobby_id,
                    organizer_id=lobby.organizer_id,
                    participant_ids=tuple(
                        participant.user_id for participant in lobby.participants
                    ),
                    create_team_voice=lobby.voice_required,
                )
                lobby = await self.ensure_match_formation_card(
                    lobby, interaction.guild, rooms
                )
            except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
                room_failure = str(exc)
            except Exception:
                deps.log.exception(
                    "Private match-room handoff failed for lobby %s", lobby_id
                )
                room_failure = "GodForge could not save the private-room handoff."
        if (
            room_failure is None
            and lobby
            and lobby.state is LobbyState.READY_CHECK
        ):
            lobby = deps.party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.FORMING,
                operation_id=f"discord:{interaction.id}:forming",
            )
            await self.refresh_public_lobby_card(lobby, interaction.guild)
        if (
            room_failure is None
            and lobby
            and lobby.state is LobbyState.FORMING
            and rooms is not None
        ):
            # One in-server ping with a real channel mention — never DMs —
            # per the Issue #63 match-ready handoff contract.
            await self._send_match_ready_handoff(lobby, interaction.guild, rooms)
        await interaction.response.edit_message(
            content=(
                "Everyone is ready. GodForge is forming the match."
                if room_failure is None
                else "Everyone is ready, but GodForge could not create temporary "
                f"rooms: {room_failure}. Fix the permission and press Ready again."
            ),
            embed=self.ready_check_embed(lobby_id, queue),
            view=(None if room_failure is None else deps.ready_check_view()),
        )

    async def handle_ready_check_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        lobby_id = self.lobby_id_from_interaction(interaction)
        status = {
            "ready": ReadyStatus.READY,
            "need_five": ReadyStatus.NEED_5,
            "drop": ReadyStatus.DROP,
        }[action]
        try:
            queue, promoted_id = await deps.party_queue_service.respond(
                lobby_id,
                interaction.user.id,
                status,
            )
        except QueueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if status is ReadyStatus.DROP:
            changed = deps.party_repository.remove_participant(
                guild_id,
                lobby_id,
                interaction.user.id,
                operation_id=f"discord:{interaction.id}:ready-drop",
                actor_id=interaction.user.id,
            )
            if promoted_id is not None:
                promoted = deps.party_repository.get_player_preferences(
                    guild_id, promoted_id
                )
                changed = deps.party_repository.save_participant(
                    guild_id,
                    lobby_id,
                    Participant(
                        promoted_id,
                        primary_role=promoted.primary_role,
                        secondary_role=promoted.secondary_role,
                        fill=promoted.fill,
                        captain=promoted.captain,
                    ),
                    operation_id=f"discord:{interaction.id}:ready-promote:{promoted_id}",
                )
            changed = await self._apply_organizer_succession(
                guild_id, lobby_id, changed, interaction.user.id,
                operation_prefix=f"discord:{interaction.id}", actor_id=interaction.user.id,
            )
            if changed.state is LobbyState.READY_CHECK:
                changed = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.OPEN,
                    operation_id=f"discord:{interaction.id}:reopen",
                )
            if changed.state in {LobbyState.OPEN, LobbyState.FULL}:
                changed = deps.party_repository.touch_recruiting_activity(
                    guild_id, lobby_id, operation_id=f"discord:{interaction.id}:touch"
                )
            if interaction.guild is not None:
                await self.refresh_public_lobby_card(changed, interaction.guild)
        everyone_ready = bool(queue.active) and all(
            queue.ready.get(member.user_id) is ReadyStatus.READY
            for member in queue.active
        )
        if everyone_ready:
            active_count = len(queue.active)
            if active_count % 2:
                await interaction.response.edit_message(
                    content=(
                        "Odd rosters cannot enter formation. Wait for one player, "
                        "drop one player, or use a substitute."
                    ),
                    embed=self.ready_check_embed(lobby_id, queue),
                    view=deps.ready_check_view(),
                )
                return
            if not 2 <= active_count <= 10:
                await interaction.response.edit_message(
                    content="Automatic formation supports 2, 4, 6, 8, or 10 active players.",
                    embed=self.ready_check_embed(lobby_id, queue),
                    view=deps.ready_check_view(),
                )
                return
            # Issue #63: serialize the "final Ready" handoff per lobby so a
            # double-click or a retried interaction can't provision two
            # rooms or send two match-ready pings.
            async with await self._handoff_lock_for(lobby_id):
                await self._complete_ready_check(interaction, guild_id, lobby_id, queue)
            return
        await interaction.response.edit_message(
            embed=self.ready_check_embed(lobby_id, queue),
            view=deps.ready_check_view(),
        )
        if promoted_id is not None:
            await interaction.followup.send(
                f"<@{promoted_id}> was promoted from the waitlist.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )

    # -- Restart recovery + periodic cleanup ---------------------------------

    async def recover_match_controls(self, ctx: LifecycleContext) -> None:
        """Reconcile durable Discord projections for every active queue.

        Issue #63: restart recovery must restore controls for *all* active
        lifecycle stages, not just forming/active matches — a recruiting
        queue's public card and an in-progress ready check both need to
        still be usable after a restart.
        """
        deps = self.deps
        for record in deps.party_repository.recover_active():
            lobby = record.lobby
            guild = ctx.get_guild(lobby.guild_id)
            if guild is None:
                continue
            try:
                if lobby.state in {LobbyState.OPEN, LobbyState.FULL}:
                    await self.ensure_public_lobby_card(lobby, guild)
                elif lobby.state is LobbyState.READY_CHECK:
                    queue = await deps.party_queue_service.get(lobby.lobby_id)
                    if queue is not None:
                        await self.ensure_ready_check_card(lobby, guild, queue)
                    await self.refresh_public_lobby_card(lobby, guild)
                elif lobby.state in {LobbyState.FORMING, LobbyState.ACTIVE}:
                    rooms = await deps.match_room_service_for_guild(guild).get(
                        lobby.lobby_id
                    )
                    if rooms is None:
                        continue
                    lobby = await self.ensure_match_formation_card(lobby, guild, rooms)
                    await self.refresh_public_lobby_card(lobby, guild)
                    if lobby.state is LobbyState.FORMING:
                        await self._send_match_ready_handoff(lobby, guild, rooms)
            except Exception:
                deps.log.exception(
                    "Restart recovery failed to restore controls for lobby %s",
                    lobby.lobby_id,
                )

    async def expire_ready_checks(self, ctx: LifecycleContext) -> None:
        """Time out ready checks whose deadline has passed.

        A timed-out queue that has been cancelled also cancels its lobby; the
        organizer's play channel (if configured) is notified either way.

        This pass also sweeps every guild's recruiting queues for the
        60-minute inactivity expiry (Issue #63): ``recover_active()``
        performs that state transition internally as a side effect of
        listing active lobbies, so simply calling it here (as the periodic
        cleanup hook) is what keeps idle queues from lingering forever even
        with no further interactions to trigger the sweep organically.
        """
        deps = self.deps
        for record in deps.party_repository.recover_active():
            lobby = record.lobby
            if lobby.state is not LobbyState.READY_CHECK:
                continue
            queue, timed_out = await deps.party_queue_service.expire(lobby.lobby_id)
            if not timed_out:
                continue
            if queue.status is QueueStatus.CANCELLED:
                deps.party_repository.transition(
                    lobby.guild_id,
                    lobby.lobby_id,
                    LobbyState.CANCELLED,
                    operation_id=f"ready-timeout:{lobby.lobby_id}:{queue.ready_deadline}",
                    reason="ready check timed out",
                )
            guild_settings = deps.settings_module.get_guild_settings(str(lobby.guild_id))
            channel_id = guild_settings["managed"].get("playChannelId")
            channel = ctx.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send(
                    "Ready check timed out for "
                    + " ".join(f"<@{user_id}>" for user_id in timed_out),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False),
                )


class PartyLobbyFeature:
    """Registers ready-check-expiry cleanup with the shared registry."""

    name = "party_lobby"

    def __init__(self, service: PartyLobbyService) -> None:
        self.service = service

    async def on_startup(self, ctx: LifecycleContext) -> None:
        await self.service.recover_match_controls(ctx)

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        await self.service.expire_ready_checks(ctx)
