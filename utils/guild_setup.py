"""Permission-safe, idempotent reconciliation for GodForge guild setup.

This module deliberately has no dependency on discord.py.  A Discord-facing
adapter supplies the small set of operations required by ``GuildSetupService``;
the service itself decides whether to reuse, repair, or create resources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class SetupStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SetupReferences:
    """Discord resource IDs persisted for one guild."""

    panel_channel_id: int | None = None
    panel_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    view_channel: bool = False
    send_messages: bool = False
    embed_links: bool = False
    read_message_history: bool = False
    manage_channels: bool = False

    def missing_panel_permissions(self) -> tuple[str, ...]:
        required = {
            "view_channel": self.view_channel,
            "send_messages": self.send_messages,
            "embed_links": self.embed_links,
            "read_message_history": self.read_message_history,
        }
        return tuple(name for name, allowed in required.items() if not allowed)


@dataclass(frozen=True, slots=True)
class SetupResult:
    status: SetupStatus
    references: SetupReferences
    actions: tuple[str, ...] = ()
    code: str | None = None
    message: str = ""
    missing_permissions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is SetupStatus.READY


class GuildSetupOperations(Protocol):
    """Injectable boundary implemented by the Discord command layer."""

    async def guild_permissions(self) -> PermissionSnapshot: ...

    async def channel_permissions(self, channel_id: int) -> PermissionSnapshot: ...

    async def channel_exists(self, channel_id: int) -> bool: ...

    async def message_exists(self, channel_id: int, message_id: int) -> bool: ...

    async def create_play_channel(self) -> int: ...

    async def ensure_channel_overwrite(self, channel_id: int) -> None: ...

    async def create_play_panel(self, channel_id: int) -> int: ...

    async def refresh_play_panel(self, channel_id: int, message_id: int) -> None: ...


class SetupOperationError(RuntimeError):
    """An adapter failure that can be safely presented to an administrator."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class GuildSetupService:
    operations: GuildSetupOperations
    _unexpected_failure_message: str = field(
        default=(
            "GodForge could not finish server setup. Check the bot role hierarchy "
            "and channel permissions, then run setup again."
        ),
        init=False,
        repr=False,
    )

    async def reconcile(self, stored: SetupReferences | None = None) -> SetupResult:
        """Reconcile the Play channel and persistent panel from stored IDs only.

        No resource is searched by name.  Missing stored resources are recreated,
        while valid resources are refreshed in place.  Repeating reconciliation
        against the returned references therefore creates no duplicates.
        """

        references = stored or SetupReferences()
        actions: list[str] = []

        try:
            channel_id = await self._resolve_channel(references, actions)
            if isinstance(channel_id, SetupResult):
                return channel_id

            if "channel_created" not in actions:
                # A freshly created channel already got the bot's own
                # overwrite atomically. A reused (already-stored) channel may
                # predate that hardening, or may have lost it to an explicit
                # permission sync — repair it on every reconcile so a plain
                # re-run of setup is enough to recover, no destructive reset
                # required.
                await self.operations.ensure_channel_overwrite(channel_id)

            permission_failure = await self._panel_permission_failure(
                channel_id, references
            )
            if permission_failure is not None:
                return permission_failure

            message_id = references.panel_message_id
            message_is_reusable = bool(
                message_id
                and references.panel_channel_id == channel_id
                and await self.operations.message_exists(channel_id, message_id)
            )
            if message_is_reusable:
                await self.operations.refresh_play_panel(channel_id, message_id)
                actions.append("panel_refreshed")
            else:
                message_id = await self.operations.create_play_panel(channel_id)
                actions.append("panel_created")

            return SetupResult(
                status=SetupStatus.READY,
                references=SetupReferences(channel_id, message_id),
                actions=tuple(actions),
                message="GodForge Play is ready.",
            )
        except SetupOperationError as exc:
            return SetupResult(
                status=SetupStatus.FAILED,
                references=references,
                actions=tuple(actions),
                code=exc.code,
                message=str(exc),
            )
        except Exception:
            return SetupResult(
                status=SetupStatus.FAILED,
                references=references,
                actions=tuple(actions),
                code="discord_operation_failed",
                message=self._unexpected_failure_message,
            )

    async def _resolve_channel(
        self, references: SetupReferences, actions: list[str]
    ) -> int | SetupResult:
        if (
            references.panel_channel_id is not None
            and await self.operations.channel_exists(references.panel_channel_id)
        ):
            return references.panel_channel_id

        guild_permissions = await self.operations.guild_permissions()
        if not guild_permissions.manage_channels:
            return SetupResult(
                status=SetupStatus.FAILED,
                references=references,
                actions=tuple(actions),
                code="missing_manage_channels",
                message=(
                    "GodForge needs Manage Channels to create its Play channel. "
                    "Grant that permission and run setup again."
                ),
                missing_permissions=("manage_channels",),
            )

        channel_id = await self.operations.create_play_channel()
        actions.append("channel_created")
        return channel_id

    async def _panel_permission_failure(
        self, channel_id: int, original: SetupReferences
    ) -> SetupResult | None:
        permissions = await self.operations.channel_permissions(channel_id)
        missing = permissions.missing_panel_permissions()
        if not missing:
            return None
        friendly = ", ".join(name.replace("_", " ").title() for name in missing)
        return SetupResult(
            status=SetupStatus.FAILED,
            references=SetupReferences(channel_id, original.panel_message_id),
            code="missing_panel_permissions",
            message=(
                f"GodForge cannot publish its Play panel in channel {channel_id}. "
                f"Grant: {friendly}, then run setup again."
            ),
            missing_permissions=missing,
        )
