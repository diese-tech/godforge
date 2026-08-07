"""discord.py adapter for temporary match-room operations."""

from __future__ import annotations

import json

import discord


class DiscordMatchRoomOperations:
    def __init__(
        self,
        guild: discord.Guild,
        category_id: int,
        archive_channel_id: int,
    ):
        self.guild = guild
        self.guild_id = guild.id
        self.category_id = category_id
        self.archive_channel_id = archive_channel_id

    async def resource_exists(self, resource_id: int) -> bool:
        return self.guild.get_channel(resource_id) is not None

    async def create_private_rooms(
        self,
        lobby_id: str,
        organizer_id: int,
        participant_ids: tuple[int, ...],
        *,
        create_team_voice: bool,
    ) -> tuple[int, int | None, int | None]:
        category = self.guild.get_channel(self.category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("The configured GodForge Rooms category is missing.")
        bot_member = self.guild.me
        overwrites = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                move_members=True,
                connect=True,
            ),
        }
        for user_id in participant_ids:
            member = self.guild.get_member(user_id)
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    connect=True,
                    speak=True,
                )
        suffix = lobby_id[:8].lower()
        created = []
        text = await self.guild.create_text_channel(
            f"match-{suffix}",
            category=category,
            overwrites=overwrites,
            reason=f"GodForge lobby {lobby_id}",
        )
        created.append(text)
        team_one = None
        team_two = None
        try:
            if create_team_voice:
                team_one = await self.guild.create_voice_channel(
                    f"Team 1 · {suffix}",
                    category=category,
                    overwrites=overwrites,
                    reason=f"GodForge lobby {lobby_id}",
                )
                created.append(team_one)
                team_two = await self.guild.create_voice_channel(
                    f"Team 2 · {suffix}",
                    category=category,
                    overwrites=overwrites,
                    reason=f"GodForge lobby {lobby_id}",
                )
                created.append(team_two)
        except Exception:
            for channel in reversed(created):
                try:
                    await channel.delete(
                        reason="Rollback incomplete GodForge room creation"
                    )
                except discord.DiscordException:
                    pass
            raise
        return (
            text.id,
            team_one.id if team_one else None,
            team_two.id if team_two else None,
        )

    async def set_locked(
        self,
        resource_ids: tuple[int, ...],
        participant_ids: tuple[int, ...],
        locked: bool,
    ) -> None:
        for resource_id in resource_ids:
            channel = self.guild.get_channel(resource_id)
            if channel:
                await channel.set_permissions(
                    self.guild.default_role,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=False,
                        connect=False,
                    ),
                    reason="GodForge organizer room control",
                )
                for user_id in participant_ids:
                    member = self.guild.get_member(user_id)
                    if member is None:
                        continue
                    await channel.set_permissions(
                        member,
                        overwrite=discord.PermissionOverwrite(
                            view_channel=True,
                            read_message_history=True,
                            send_messages=not locked,
                            connect=not locked,
                            speak=not locked,
                        ),
                        reason="GodForge organizer room control",
                    )

    async def remove_player(
        self, resource_ids: tuple[int, ...], user_id: int
    ) -> None:
        member = self.guild.get_member(user_id)
        if member is None:
            return
        for resource_id in resource_ids:
            channel = self.guild.get_channel(resource_id)
            if channel:
                await channel.set_permissions(
                    member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=False, connect=False
                    ),
                    reason="Removed from GodForge lobby",
                )

    async def sync_participants(
        self,
        resource_ids: tuple[int, ...],
        participant_ids: tuple[int, ...],
        removed_ids: tuple[int, ...],
        locked: bool,
    ) -> None:
        for user_id in removed_ids:
            await self.remove_player(resource_ids, user_id)
        for resource_id in resource_ids:
            channel = self.guild.get_channel(resource_id)
            if channel is None:
                continue
            for user_id in participant_ids:
                member = self.guild.get_member(user_id)
                if member is None:
                    continue
                await channel.set_permissions(
                    member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=not locked,
                        connect=not locked,
                        speak=not locked,
                    ),
                    reason="GodForge continuity roster reconciliation",
                )

    async def transfer_organizer(
        self,
        resource_ids: tuple[int, ...],
        old_organizer_id: int,
        new_organizer_id: int,
    ) -> None:
        # Organizer authority stays in GodForge's durable record. No Discord
        # Manage Channels permission is granted to either member.
        return None

    async def move_from_lobby_voice(
        self, user_id: int, lobby_voice_id: int | None, destination_id: int
    ) -> str | None:
        member = self.guild.get_member(user_id)
        destination = self.guild.get_channel(destination_id)
        if member is None or not isinstance(destination, discord.VoiceChannel):
            return "Player or destination voice room is unavailable."
        if member.voice is None or member.voice.channel is None:
            return "Player is not connected to voice."
        if lobby_voice_id is not None and member.voice.channel.id != lobby_voice_id:
            return "Player is not in the configured lobby voice channel."
        if not destination.permissions_for(self.guild.me).move_members:
            return "GodForge needs Move Members in the temporary-room category."
        try:
            await member.move_to(destination, reason="GodForge team room assignment")
        except discord.Forbidden:
            return "Discord denied the move; check GodForge's role and Move Members permission."
        except discord.HTTPException:
            return "Discord could not move this player. Try again."
        return None

    async def archive_summary(self, summary: dict) -> int | None:
        channel = self.guild.get_channel(self.archive_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("The GodForge Play archive channel is unavailable.")
        message = await channel.send(
            "GodForge room summary\n```json\n"
            + json.dumps(summary, sort_keys=True, indent=2)
            + "\n```",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return message.id

    async def delete_resources(self, resource_ids: tuple[int, ...]) -> None:
        for resource_id in resource_ids:
            channel = self.guild.get_channel(resource_id)
            if channel:
                try:
                    await channel.delete(reason="GodForge temporary room cleanup")
                except discord.NotFound:
                    pass
