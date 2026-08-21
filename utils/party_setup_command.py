"""The `/party setup` zero-config provisioning command: Discord adapter.

Feature module for the Issue #48 refactor. Owns `party setup` plus its Discord
operations adapter (``DiscordGuildSetupOperations``), the Play-panel embed, and
room-category provisioning — previously defined directly in ``bot.py``. Domain
orchestration stays in ``utils/guild_setup.GuildSetupService``; this module
wires that service to real Discord guild/channel/role operations.

The Play panel's persistent view is injected (``play_panel_view``) rather than
imported directly, since its action handler belongs to the play-panel feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands

from utils.guild_setup import (
    GuildSetupService,
    PermissionSnapshot,
    SetupOperationError,
    SetupReferences,
)
from utils.managed_roles import ManagedRoleError, reconcile as reconcile_roles


def play_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="Play SMITE with GodForge",
        description=(
            "Create or join a reusable party lobby, browse active groups, or "
            "set the roles you prefer to play."
        ),
        color=0x3498DB,
    )


class DiscordGuildSetupOperations:
    """Adapts ``GuildSetupService``'s operations protocol to a real guild."""

    def __init__(self, guild: discord.Guild, play_panel_view: Callable[[], discord.ui.View]):
        self.guild = guild
        self._play_panel_view = play_panel_view

    async def guild_permissions(self) -> PermissionSnapshot:
        permissions = self.guild.me.guild_permissions
        return PermissionSnapshot(manage_channels=permissions.manage_channels)

    async def channel_permissions(self, channel_id: int) -> PermissionSnapshot:
        channel = self.guild.get_channel(channel_id)
        if channel is None:
            return PermissionSnapshot()
        permissions = channel.permissions_for(self.guild.me)
        return PermissionSnapshot(
            view_channel=permissions.view_channel,
            send_messages=permissions.send_messages,
            embed_links=permissions.embed_links,
            read_message_history=permissions.read_message_history,
            manage_channels=permissions.manage_channels,
        )

    async def channel_exists(self, channel_id: int) -> bool:
        return self.guild.get_channel(channel_id) is not None

    async def message_exists(self, channel_id: int, message_id: int) -> bool:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise SetupOperationError(
                "panel_read_forbidden",
                "GodForge cannot verify its stored Play panel. Grant Read "
                "Message History and run setup again.",
            ) from exc
        except discord.HTTPException as exc:
            raise SetupOperationError(
                "panel_check_failed",
                "Discord could not verify the stored Play panel. No duplicate "
                "was created; retry setup shortly.",
            ) from exc
        return True

    async def create_play_channel(self) -> int:
        conflict = discord.utils.get(self.guild.text_channels, name="godforge-play")
        if conflict is not None:
            raise SetupOperationError(
                "channel_name_conflict",
                "A channel named #godforge-play already exists but is not managed "
                "by GodForge. Rename it or explicitly adopt it before retrying.",
            )
        channel = await self.guild.create_text_channel(
            "godforge-play",
            reason="GodForge zero-config setup",
        )
        return channel.id

    async def create_play_panel(self, channel_id: int) -> int:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise SetupOperationError(
                "invalid_play_channel",
                "The stored GodForge Play channel is not a text channel.",
            )
        message = await channel.send(embed=play_panel_embed(), view=self._play_panel_view())
        return message.id

    async def refresh_play_panel(self, channel_id: int, message_id: int) -> None:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise SetupOperationError(
                "invalid_play_channel",
                "The stored GodForge Play channel is not a text channel.",
            )
        message = await channel.fetch_message(message_id)
        await message.edit(embed=play_panel_embed(), view=self._play_panel_view())


async def ensure_room_category(guild: discord.Guild, stored_category_id: str) -> int:
    if stored_category_id:
        category = guild.get_channel(int(stored_category_id))
        if isinstance(category, discord.CategoryChannel):
            return category.id
    conflict = discord.utils.get(guild.categories, name="GodForge Rooms")
    if conflict is not None:
        raise SetupOperationError(
            "category_name_conflict",
            "A category named GodForge Rooms already exists but is not managed "
            "by GodForge. Rename it or explicitly adopt it before retrying.",
        )
    return (
        await guild.create_category(
            "GodForge Rooms",
            reason="GodForge temporary party rooms",
        )
    ).id


@dataclass
class PartySetupCommandDeps:
    """Collaborators injected into the `/party setup` command."""

    settings_module: object
    reconcile_roles: Callable = reconcile_roles
    play_panel_view: Callable[[], discord.ui.View] = None


def register_party_setup_command(
    group: app_commands.Group, deps: PartySetupCommandDeps
) -> None:
    """Register `setup` onto the existing `/party` group.

    Also attached as ``group.setup`` for direct invocation in tests.
    """

    @group.command(name="setup", description="Set up GodForge for this server")
    @app_commands.describe(
        test_mode="Use short-lived lobbies without recording match history",
        captain_role="Create an optional self-assignable captain role",
        substitute_role="Create an optional substitute role",
        region_role="Create an optional region role",
        lfg_role="Create an optional LFG notification role",
    )
    async def setup(
        interaction: discord.Interaction,
        test_mode: bool = False,
        captain_role: bool = False,
        substitute_role: bool = False,
        region_role: bool = False,
        lfg_role: bool = False,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this command inside a Discord server.",
                ephemeral=True,
            )
            return
        if not getattr(interaction.user.guild_permissions, "manage_guild", False):
            await interaction.response.send_message(
                "You need Manage Server to configure GodForge.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        settings = deps.settings_module
        current = settings.get_guild_settings(str(guild.id))
        managed = current["managed"]
        try:
            enabled_role_keys = ["solo", "jungle", "mid", "support", "adc"]
            enabled_role_keys.extend(
                key
                for key, enabled in (
                    ("captain", captain_role),
                    ("substitute", substitute_role),
                    ("region", region_role),
                    ("lfg", lfg_role),
                )
                if enabled
            )
            role_result = await deps.reconcile_roles(
                guild,
                managed["roleIds"],
                enabled_keys=enabled_role_keys,
            )
            settings.update_guild_settings(
                str(guild.id),
                {
                    "managed": {
                        "roleIds": {
                            key: str(role_id)
                            for key, role_id in role_result.role_ids.items()
                        }
                    }
                },
                updated_by=f"discord:{interaction.user.id}",
            )
            category_id = await ensure_room_category(
                guild,
                managed.get("roomCategoryId", ""),
            )
            settings.update_guild_settings(
                str(guild.id),
                {"managed": {"roomCategoryId": str(category_id)}},
                updated_by=f"discord:{interaction.user.id}",
            )
            setup_result = await GuildSetupService(
                DiscordGuildSetupOperations(guild, deps.play_panel_view)
            ).reconcile(
                SetupReferences(
                    int(managed["playChannelId"]) if managed["playChannelId"] else None,
                    int(managed["playMessageId"]) if managed["playMessageId"] else None,
                )
            )
        except (ManagedRoleError, SetupOperationError, discord.DiscordException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        settings.update_guild_settings(
            str(guild.id),
            {
                "managed": {
                    "playChannelId": (
                        str(setup_result.references.panel_channel_id)
                        if setup_result.references.panel_channel_id
                        else ""
                    ),
                    "playMessageId": (
                        str(setup_result.references.panel_message_id)
                        if setup_result.references.panel_message_id
                        else ""
                    ),
                }
            },
            updated_by=f"discord:{interaction.user.id}",
        )
        if not setup_result.ok:
            await interaction.followup.send(setup_result.message, ephemeral=True)
            return
        settings.update_guild_settings(
            str(guild.id),
            {
                "managed": {
                    "playChannelId": str(setup_result.references.panel_channel_id),
                    "playMessageId": str(setup_result.references.panel_message_id),
                    "roomCategoryId": str(category_id),
                    "roleIds": {
                        key: str(role_id)
                        for key, role_id in role_result.role_ids.items()
                    },
                    "testMode": test_mode,
                }
            },
            updated_by=f"discord:{interaction.user.id}",
        )
        created_roles = ", ".join(role_result.created_keys) or "none"
        await interaction.followup.send(
            f"GodForge Play is ready. Created roles: {created_roles}. "
            f"Test mode: {'on' if test_mode else 'off'}.",
            ephemeral=True,
        )

    group.setup = setup
