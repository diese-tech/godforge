"""Characterization tests for /party setup in bot.py.

Written before extracting this handler into a feature module (Issue #48,
Phase 5c). Pins down current orchestration behavior — permission gating, role
reconciliation, room-category creation, and Play-panel setup — using a mocked
Discord guild. GuildSetupService and managed-role reconciliation already have
their own dedicated unit tests; this focuses on party_setup's own wiring.
"""

from unittest.mock import AsyncMock, MagicMock

import discord
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
    channel.set_permissions = AsyncMock()
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
            ch.set_permissions = channel.set_permissions
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


async def test_play_channel_created_with_a_bot_self_overwrite(tmp_settings):
    # A channel-specific overwrite for the bot survives a plain category
    # move (Discord only clears it if someone explicitly syncs
    # permissions), unlike purely inherited/ambient access.
    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_setup.callback(interaction)
    kwargs = guild.create_text_channel.await_args.kwargs
    overwrite = kwargs["overwrites"][guild.me]
    assert overwrite.view_channel is True
    assert overwrite.send_messages is True
    assert overwrite.embed_links is True
    assert overwrite.read_message_history is True


async def test_reused_play_channel_gets_its_overwrite_repaired_on_rerun(tmp_settings):
    # A channel /party setup created before this hardening existed (or one
    # that lost its own overwrite to an explicit permission sync) never goes
    # through create_play_channel again on a later run, since the stored
    # channel is simply reused. A plain re-run of setup must still repair
    # it — no destructive /party reset should be required to recover.
    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_setup.callback(interaction)  # first run: creates + stores
    guild.create_text_channel.reset_mock()
    guild.get_channel(6000).set_permissions.reset_mock()

    interaction2 = _interaction(guild)
    await bot.party_setup.callback(interaction2)  # second run: reuses stored id

    guild.create_text_channel.assert_not_awaited()
    kwargs = guild.get_channel(6000).set_permissions.await_args.kwargs
    assert kwargs["overwrite"].view_channel is True
    assert kwargs["overwrite"].send_messages is True
    assert kwargs["overwrite"].embed_links is True
    assert kwargs["overwrite"].read_message_history is True


# -- /party reset ---------------------------------------------------------


async def test_reset_requires_guild():
    interaction = _interaction(None)
    interaction.guild = None
    await bot.party_reset.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Discord server" in reply


async def test_reset_requires_manage_guild_permission():
    guild = _guild()
    interaction = _interaction(guild, manage_guild=False)
    await bot.party_reset.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in reply


async def test_reset_with_nothing_stored_reports_nothing_to_do(tmp_settings):
    guild = _guild()
    interaction = _interaction(guild)
    await bot.party_reset.callback(interaction)
    reply = interaction.followup.send.call_args.args[0]
    assert "Nothing is currently stored" in reply


async def test_reset_dry_run_previews_without_deleting_or_clearing(tmp_settings):
    from utils import settings as settings_mod

    settings_mod.update_guild_settings(
        "1",
        {
            "managed": {
                "playChannelId": "6000", "playMessageId": "7000",
                "roomCategoryId": "5000", "roleIds": {"solo": "8001"},
            }
        },
    )
    guild = _guild()
    interaction = _interaction(guild)

    await bot.party_reset.callback(interaction, confirm=False)

    reply = interaction.followup.send.call_args.args[0]
    assert "Play channel" in reply
    assert "Room category" in reply
    assert "Nothing was changed" in reply
    saved = settings_mod.get_guild_settings("1")
    assert saved["managed"]["playChannelId"] == "6000"
    assert saved["managed"]["roleIds"]["solo"] == "8001"


async def test_reset_confirmed_deletes_resources_and_clears_settings(tmp_settings):
    from utils import settings as settings_mod

    settings_mod.update_guild_settings(
        "1",
        {
            "managed": {
                "playChannelId": "6000", "playMessageId": "7000",
                "roomCategoryId": "5000",
                "roleIds": {"solo": "8001", "jungle": "8002"},
            }
        },
    )
    guild = _guild()
    channel = MagicMock(id=6000)
    channel.delete = AsyncMock()
    category = MagicMock(id=5000)
    category.delete = AsyncMock()
    role_solo = MagicMock(id=8001)
    role_solo.delete = AsyncMock()
    guild.get_channel = lambda cid: {6000: channel, 5000: category}.get(cid)
    # The jungle role was already deleted out-of-band — resolves to None.
    guild.get_role = lambda rid: role_solo if rid == 8001 else None
    interaction = _interaction(guild)

    await bot.party_reset.callback(interaction, confirm=True)

    channel.delete.assert_awaited_once()
    category.delete.assert_awaited_once()
    role_solo.delete.assert_awaited_once()
    saved = settings_mod.get_guild_settings("1")
    assert saved["managed"]["playChannelId"] == ""
    assert saved["managed"]["playMessageId"] == ""
    assert saved["managed"]["roomCategoryId"] == ""
    assert saved["managed"]["roleIds"]["solo"] == ""
    assert saved["managed"]["roleIds"]["jungle"] == ""
    reply = interaction.followup.send.call_args.args[0]
    assert "reset" in reply.lower()
    assert "already gone" in reply.lower()


async def test_reset_reports_deletion_failures_but_still_clears_settings(tmp_settings):
    from utils import settings as settings_mod

    settings_mod.update_guild_settings("1", {"managed": {"playChannelId": "6000"}})
    guild = _guild()
    channel = MagicMock(id=6000)
    channel.delete = AsyncMock(side_effect=discord.DiscordException("Missing Permissions"))
    guild.get_channel = lambda cid: channel if cid == 6000 else None
    guild.get_role = lambda rid: None
    interaction = _interaction(guild)

    await bot.party_reset.callback(interaction, confirm=True)

    saved = settings_mod.get_guild_settings("1")
    assert saved["managed"]["playChannelId"] == ""
    reply = interaction.followup.send.call_args.args[0]
    assert "Could not delete" in reply
    assert "Missing Permissions" in reply
