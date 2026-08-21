"""Characterization tests for /party setup in bot.py.

Written before extracting this handler into a feature module (Issue #48,
Phase 5c). Pins down current orchestration behavior — permission gating, role
reconciliation, room-category creation, and Play-panel setup — using a mocked
Discord guild. GuildSetupService and managed-role reconciliation already have
their own dedicated unit tests; this focuses on party_setup's own wiring.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


def _guild(*, manage_channels=True):
    guild = MagicMock()
    guild.id = 1
    guild.categories = []
    guild.text_channels = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_channels = manage_channels
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role.position = 100

    def create_role(**kwargs):
        role = MagicMock()
        role.name = kwargs.get("name")
        role.position = 1
        role.is_default = lambda: False
        return role

    guild.create_role = AsyncMock(side_effect=create_role)
    guild.get_role = lambda role_id: None

    category = MagicMock(id=5000)
    guild.create_category = AsyncMock(return_value=category)

    channel = MagicMock(id=6000)
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True,
        read_message_history=True, manage_channels=True,
    )
    panel_message = MagicMock(id=7000)
    channel.send = AsyncMock(return_value=panel_message)
    guild.create_text_channel = AsyncMock(return_value=channel)

    def get_channel(channel_id):
        if channel_id == category.id:
            import discord
            cat = MagicMock(spec=discord.CategoryChannel)
            cat.id = category.id
            return cat
        if channel_id == channel.id:
            import discord
            ch = MagicMock(spec=discord.TextChannel)
            ch.id = channel.id
            ch.send = channel.send
            ch.permissions_for = channel.permissions_for
            ch.fetch_message = AsyncMock(side_effect=Exception("not found"))
            return ch
        return None

    guild.get_channel = get_channel
    return guild


def _interaction(guild, *, manage_guild=True):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild = guild
    interaction.user = MagicMock()
    interaction.user.id = 100
    interaction.user.guild_permissions.manage_guild = manage_guild
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_requires_guild():
    interaction = _interaction(None)
    interaction.guild = None
    await bot.party_setup.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Discord server" in reply


async def test_requires_manage_guild_permission():
    guild = _guild()
    interaction = _interaction(guild, manage_guild=False)
    await bot.party_setup.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in reply


async def test_setup_creates_category_and_panel(tmp_settings):
    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_setup.callback(interaction)
    interaction.response.defer.assert_awaited_once()
    guild.create_category.assert_awaited_once()
    guild.create_text_channel.assert_awaited_once()
    reply = interaction.followup.send.call_args.args[0]
    assert "GodForge Play is ready" in reply


async def test_setup_persists_managed_settings(tmp_settings):
    from utils import settings as settings_mod

    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_setup.callback(interaction)
    saved = settings_mod.get_guild_settings(str(guild.id))
    assert saved["managed"]["roomCategoryId"] == "5000"
    assert saved["managed"]["playChannelId"] == "6000"
    assert saved["managed"]["playMessageId"] == "7000"


async def test_setup_test_mode_flag_is_stored(tmp_settings):
    from utils import settings as settings_mod

    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_setup.callback(interaction, test_mode=True)
    saved = settings_mod.get_guild_settings(str(guild.id))
    assert saved["managed"]["testMode"] is True
