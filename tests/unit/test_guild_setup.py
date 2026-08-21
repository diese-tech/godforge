from __future__ import annotations

import pytest

from utils.guild_setup import (
    GuildSetupService,
    PermissionSnapshot,
    SetupOperationError,
    SetupReferences,
    SetupStatus,
)


FULL_PERMISSIONS = PermissionSnapshot(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    manage_channels=True,
)


class FakeOperations:
    def __init__(self):
        self.guild_perms = FULL_PERMISSIONS
        self.channel_perms = FULL_PERMISSIONS
        self.channels: set[int] = set()
        self.messages: set[tuple[int, int]] = set()
        self.created_channels = 0
        self.created_panels = 0
        self.refreshed: list[tuple[int, int]] = []
        self.failure: SetupOperationError | None = None
        self.overwrite_repairs: list[int] = []

    async def guild_permissions(self):
        return self.guild_perms

    async def channel_permissions(self, channel_id):
        return self.channel_perms

    async def channel_exists(self, channel_id):
        return channel_id in self.channels

    async def message_exists(self, channel_id, message_id):
        return (channel_id, message_id) in self.messages

    async def create_play_channel(self):
        if self.failure:
            raise self.failure
        self.created_channels += 1
        self.channels.add(100)
        return 100

    async def ensure_channel_overwrite(self, channel_id):
        self.overwrite_repairs.append(channel_id)

    async def create_play_panel(self, channel_id):
        self.created_panels += 1
        self.messages.add((channel_id, 200))
        return 200

    async def refresh_play_panel(self, channel_id, message_id):
        self.refreshed.append((channel_id, message_id))


@pytest.mark.asyncio
async def test_first_setup_creates_channel_and_panel():
    operations = FakeOperations()

    result = await GuildSetupService(operations).reconcile()

    assert result.status is SetupStatus.READY
    assert result.references == SetupReferences(100, 200)
    assert result.actions == ("channel_created", "panel_created")
    # The freshly created channel already got the overwrite atomically at
    # creation; reconciling it again would be a redundant Discord call.
    assert operations.overwrite_repairs == []


@pytest.mark.asyncio
async def test_reconcile_uses_stored_ids_and_is_idempotent():
    operations = FakeOperations()
    operations.channels.add(123)
    operations.messages.add((123, 456))

    result = await GuildSetupService(operations).reconcile(SetupReferences(123, 456))

    assert result.ok
    assert result.references == SetupReferences(123, 456)
    assert result.actions == ("panel_refreshed",)
    assert operations.created_channels == 0
    assert operations.created_panels == 0
    assert operations.refreshed == [(123, 456)]


@pytest.mark.asyncio
async def test_reused_channel_gets_its_overwrite_repaired_every_reconcile():
    # A stored channel from before this hardening existed (or one that lost
    # its overwrite to an explicit permission sync) never goes through
    # create_play_channel again, since _resolve_channel only creates when
    # nothing is stored. A plain re-run of /party setup must still be enough
    # to recover it, with no destructive reset required.
    operations = FakeOperations()
    operations.channels.add(123)
    operations.messages.add((123, 456))

    result = await GuildSetupService(operations).reconcile(SetupReferences(123, 456))

    assert result.ok
    assert operations.overwrite_repairs == [123]
    assert operations.created_channels == 0


@pytest.mark.asyncio
async def test_deleted_message_is_recreated_in_stored_channel():
    operations = FakeOperations()
    operations.channels.add(123)

    result = await GuildSetupService(operations).reconcile(SetupReferences(123, 456))

    assert result.references == SetupReferences(123, 200)
    assert result.actions == ("panel_created",)
    assert operations.created_channels == 0


@pytest.mark.asyncio
async def test_deleted_channel_does_not_reuse_message_from_old_channel():
    operations = FakeOperations()
    operations.messages.add((100, 456))

    result = await GuildSetupService(operations).reconcile(SetupReferences(123, 456))

    assert result.references == SetupReferences(100, 200)
    assert result.actions == ("channel_created", "panel_created")


@pytest.mark.asyncio
async def test_missing_manage_channels_returns_actionable_failure_without_mutation():
    operations = FakeOperations()
    operations.guild_perms = PermissionSnapshot()

    result = await GuildSetupService(operations).reconcile()

    assert not result.ok
    assert result.code == "missing_manage_channels"
    assert result.missing_permissions == ("manage_channels",)
    assert "run setup again" in result.message
    assert operations.created_channels == 0


@pytest.mark.asyncio
async def test_existing_channel_preflight_lists_missing_panel_permissions():
    operations = FakeOperations()
    operations.channels.add(123)
    operations.channel_perms = PermissionSnapshot(view_channel=True)

    result = await GuildSetupService(operations).reconcile(SetupReferences(123, None))

    assert result.code == "missing_panel_permissions"
    assert result.references.panel_channel_id == 123
    assert result.missing_permissions == (
        "send_messages",
        "embed_links",
        "read_message_history",
    )
    assert operations.created_panels == 0


@pytest.mark.asyncio
async def test_adapter_failure_is_structured_and_safe_to_display():
    operations = FakeOperations()
    operations.failure = SetupOperationError(
        "channel_creation_forbidden",
        "Discord rejected channel creation. Move the GodForge role higher.",
    )

    result = await GuildSetupService(operations).reconcile()

    assert result.status is SetupStatus.FAILED
    assert result.code == "channel_creation_forbidden"
    assert result.message == (
        "Discord rejected channel creation. Move the GodForge role higher."
    )
