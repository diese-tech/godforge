"""
GodForge — Smite 2 Discord Bot

Session system: when a session is active in a channel, .rg and .roll5
produce interactive embeds with reactions for tracking random god picks.

Draft system: integrates with an Activity backend to facilitate competitive
drafting. Captains participate in a Discord Activity while the bot mirrors
draft state as a live, updating embed in the channel.

Sessions and drafts are mutually exclusive per channel.

Run with: python bot.py
"""

import asyncio
import io
import json
import logging
import os
import re

import aiohttp
import discord
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from utils import custom_commands, draft_support, formatter, loader, parser, picker, settings
from utils.formatter import NUMBER_EMOJIS
from utils.resolver import resolve_god_name
from utils.session import SessionManager
from utils.draft import DraftManager
from utils.forgelens_adapter import ForgeLensAdapter, forgelens_enabled
from utils.party_store import SQLitePartyRepository
from utils.party import LobbyState, Participant, PlayerPreferences
from utils.guild_setup import (
    GuildSetupService,
    PermissionSnapshot,
    SetupOperationError,
    SetupReferences,
)
from utils.managed_roles import ManagedRoleError, reconcile as reconcile_roles, set_member_role
from utils.setup_views import PlayPanelView, RolePreferencesView
from utils.lobby_views import (
    AlreadyInQueueView,
    ChangeRolesPromptView,
    CreateLobbyWizardView,
    JoinPreferencesView,
    LobbyCardView,
    MatchFormationView,
    MatchContinuityView,
    MatchResultView,
    QueueSelectView,
    QueueSettingsView,
    ReadyCheckView,
    RecruitingCardView,
    RenameQueueModal,
    StartQueueModal,
    TransferOrganizerView,
)
from utils.party_queue import (
    PartyQueueService,
    QueueError,
    QueueStatus,
    ReadyStatus,
    SQLitePartyQueueRepository,
)
from utils.party_draft import PartyDraftError, PartyDraftLaunchRepository
from utils.party_schedule import (
    Recurrence,
    ScheduleError,
    ScheduleRepository,
    calendar_ics,
    convert_to_lobby,
    parse_local_start,
)
from utils.scrims import (
    ChallengeState,
    ScrimError,
    ScrimRepository,
    launch_scrim,
)
from utils.activity_backend import ActivityBackendClient
from utils.active_drafts import ActiveDraftStore
from utils.custom_command_runtime import CustomCommandRuntime
from utils.draft_coordinator import DraftCoordinator, DraftFeature
from utils.room_lifecycle import RoomLifecycle
from utils.schedule_lifecycle import ScheduleLifecycle
from utils.draft_render import DraftRenderer
from utils import match_results
from utils.match_room_factory import MatchRoomServiceFactory
from utils.match_actions import (
    MatchActionDeps,
    handle_match_continuity_action,
    handle_match_result_action,
)
from utils.party_lobby import PartyLobbyDeps, PartyLobbyFeature, PartyLobbyService
from utils.party_room_command import PartyRoomCommandDeps, register_party_room_command
from utils.party_setup_command import PartySetupCommandDeps, register_party_setup_command
from utils.schedule_commands import ScheduleCommandDeps, register_schedule_commands
from utils.scrim_commands import ScrimCommandDeps, register_scrim_commands
from utils.session_commands import SessionCommandHandler
from utils.lifecycle import FeatureRegistry, LifecycleContext
from utils.routing import CommandRegistry
from utils.r67.feature import R67Feature
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service, build_survivor_announcement
from utils.match_history import (
    MatchHistoryRepository,
    MatchOutcome,
    MatchPlayer,
    MatchTeam,
)
from utils.match_continuity import (
    ContinuityError,
    ContinuityStatus,
    MatchContinuityRepository,
    MatchContinuityService,
)
from utils.team_formation import FormationMode
from utils import match_ids
from utils.match_rooms import MatchRoomService, SQLiteMatchRoomRepository
from utils.discord_match_rooms import DiscordMatchRoomOperations
# Legacy economy (wallets, ledger, matches, betting) is dormant — DO NOT
# DELETE — kept for possible future reuse and relocated to
# utils/legacy_economy.py for organization (Issue #48). Re-exported here so
# existing call sites/tests that reference these as bot.<name> keep working.
from utils.legacy_economy import (
    _extract_team_names,
    _find_matching_team,
    _extract_player_name,
    _post_wallets_to_reports,
    BettingLedgerView,
    _build_ledger_embed,
    _handle_wallet_command,
    _wallet_adjust,
    _wallet_check,
    _wallet_wipe,
    _handle_ledger_command,
    _ledger_reset,
    _handle_match_command,
    _match_create,
    _match_draft,
    _match_resolve,
    _match_resolve_winner,
    _match_resolve_prop,
    _handle_bet_command,
    _place_win_bet,
    _place_prop_bet,
    update_betting_embed,
)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
ACTIVITY_BACKEND_URL = os.getenv("ACTIVITY_BACKEND_URL", "").rstrip("/")
ACTIVITY_API_KEY = os.getenv("ACTIVITY_API_KEY", "")
LEGACY_ECONOMY_ENABLED = os.getenv("GODFORGE_ENABLE_LEGACY_ECONOMY", "").strip().lower() in {
    "1", "true", "yes", "on"
}

# Channel IDs for the betting system.
# Set these in .env (or leave 0 to disable that feature).
BETTING_LEDGER_CHANNEL_ID = int(os.getenv("BETTING_LEDGER_CHANNEL_ID", "0"))
PLACE_BETS_CHANNEL_ID = int(os.getenv("PLACE_BETS_CHANNEL_ID", "0"))
MATCH_DRAFT_CHANNEL_ID = int(os.getenv("MATCH_DRAFT_CHANNEL_ID", "0"))

_GOD_USER_ID = int(os.getenv("GODFORGE_OWNER_USER_ID", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("godforge")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True


class GodForgeClient(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PlayPanelView(_handle_play_panel_action))
        self.add_view(RolePreferencesView(_handle_role_preference))
        self.add_view(LobbyCardView(_handle_lobby_card_action))
        # Issue #63: the public queue card while recruiting uses a smaller
        # button set (Join/Leave/Settings/Cancel) than LobbyCardView above,
        # but shares the same custom_id prefix/action names for the buttons
        # both expose — this registers the one new custom_id it adds
        # (queue_settings) so it keeps routing correctly after a restart.
        self.add_view(RecruitingCardView(_handle_lobby_card_action))
        self.add_view(ReadyCheckView(_handle_ready_check_action))
        self.add_view(MatchFormationView(_handle_lobby_card_action))
        self.add_view(MatchResultView(_handle_match_result_action))
        self.add_view(MatchContinuityView(_handle_match_continuity_action))
        self.add_view(ScrimChallengeView())
        await self.tree.sync()


client = GodForgeClient(intents=intents)

sessions = SessionManager()
drafts = DraftManager()
_PARTY_DB_PATH = os.getenv("GODFORGE_PARTY_DB_PATH", "data/godforge_party.db")
party_repository = SQLitePartyRepository(_PARTY_DB_PATH)
party_queue_service = PartyQueueService(
    SQLitePartyQueueRepository(
        _PARTY_DB_PATH
    )
)
party_draft_repository = PartyDraftLaunchRepository(_PARTY_DB_PATH)
match_history_repository = MatchHistoryRepository(_PARTY_DB_PATH)
match_continuity_repository = MatchContinuityRepository(_PARTY_DB_PATH)
match_room_repository = SQLiteMatchRoomRepository(_PARTY_DB_PATH)
schedule_repository = ScheduleRepository(_PARTY_DB_PATH)
scrim_repository = ScrimRepository(_PARTY_DB_PATH)
r67_repository = SQLiteR67Repository(_PARTY_DB_PATH)
r67_service = R67Service(r67_repository)
forgelens_adapter = ForgeLensAdapter()

# Shared feature lifecycle registry (Issue #48). Features own their recovery and
# cleanup and register it here; bot.py only orchestrates the shared phases.
feature_registry = FeatureRegistry()
feature_registry.register(R67Feature(r67_service))
feature_registry.register(ScheduleLifecycle(schedule_repository))


def _lifecycle_context() -> LifecycleContext:
    return LifecycleContext(
        get_guild=client.get_guild,
        get_channel=client.get_channel,
        get_user=client.get_user,
        fetch_user=client.fetch_user,
        guilds=tuple(client.guilds),
    )


# Shared dot-command routing seam (Issue #48). Features register the exact
# command token(s) they own; on_message resolves them in one lookup.
command_registry = CommandRegistry()

# Track metadata for reaction-enabled messages (sessions only).
_tracked_messages = {}

# Durable local-draft restart pointer is owned by ActiveDraftStore (Issue #48).
active_draft_store = ActiveDraftStore()


def _save_active_draft(channel_id: int, draft_id: str):
    active_draft_store.save(channel_id, draft_id)


def _remove_active_draft(channel_id: int):
    active_draft_store.remove(channel_id)

REPORTS_CHANNELS = {
    int(gid.strip()): int(cid.strip())
    for pair in os.getenv("GODFORGE_REPORTS_CHANNELS", "").split(",")
    if ":" in pair
    for gid, cid in [pair.split(":", 1)]
    if gid.strip() and cid.strip()
}


def _channel_has_active(channel_id: int) -> str | None:
    # Session state is bot-owned; draft state is owned by the draft coordinator.
    if sessions.get(channel_id):
        return "session"
    if draft_coordinator.has_active_draft(channel_id):
        return "draft"
    return None


def _cleanup_draft(channel_id: int) -> None:
    draft_coordinator.cleanup_draft(channel_id)


# Pure draft-command helpers are owned by utils/draft_support (Issue #48).
def _draft_start_options(content: str) -> dict:
    return draft_support.draft_start_options(content)


def _draft_completion_marker(draft) -> str:
    return draft_support.draft_completion_marker(draft)


# ── Activity backend helpers ──────────────────────────────────────────────────
# HTTP access to the optional draft activity backend is owned by the client
# (Issue #48); bot.py keeps thin delegators for the existing call sites.
activity_client = ActivityBackendClient(ACTIVITY_BACKEND_URL, ACTIVITY_API_KEY)


def _activity_headers() -> dict:
    return activity_client.headers()


async def _activity_post(path: str, data: dict | None = None) -> dict | None:
    return await activity_client.post(path, data)


async def _activity_get(path: str) -> dict | None:
    return await activity_client.get(path)


# Activity-draft rendering, export, and the WS listener are owned by the draft
# coordinator (Issue #48). bot.py keeps thin delegators for existing call sites.
async def _update_embed_from_snapshot(snapshot: dict, channel) -> None:
    await draft_coordinator.update_embed_from_snapshot(snapshot, channel)


async def _post_export(export: dict, channel) -> None:
    await draft_coordinator.post_export(export, channel)


async def _listen_draft_ws(match_id: str, channel_id: int) -> None:
    await draft_coordinator.listen_ws(match_id, channel_id)


# ── Discord events ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (id: {client.user.id})")
    log.info(f"Connected to {len(client.guilds)} guild(s)")
    if not cleanup_task.is_running():
        cleanup_task.start()
    log.info("Economy and betting commands are disabled in standalone GodForge.")
    recoverable_lobbies = party_repository.recover_active()
    if recoverable_lobbies:
        log.info("Recovered %s active party lobby record(s)", len(recoverable_lobbies))

    await feature_registry.run_startup(_lifecycle_context())


@tasks.loop(minutes=5)
async def cleanup_task():
    expired_sessions = sessions.cleanup_expired()
    if expired_sessions:
        to_remove = [mid for mid, info in _tracked_messages.items()
                     if info.get("channel_id") in expired_sessions]
        for mid in to_remove:
            del _tracked_messages[mid]
        log.info(f"Cleaned up {len(expired_sessions)} expired session(s)")

    expired_drafts = drafts.cleanup_expired()
    if expired_drafts:
        log.info(f"Cleaned up {len(expired_drafts)} expired local draft(s)")

    await feature_registry.run_cleanup(_lifecycle_context())


@cleanup_task.error
async def cleanup_task_error(exc: Exception):
    log.error(f"cleanup_task crashed: {exc!r} — restarting")
    cleanup_task.restart()


@client.event
async def on_voice_state_update(member, before, after):
    changed_ids = {
        channel.id
        for channel in (before.channel, after.channel)
        if channel is not None
    }
    if not changed_ids:
        return
    for rooms in match_room_repository.active(member.guild.id):
        if not changed_ids.intersection(rooms.team_voice_ids):
            continue
        guild = member.guild
        voice_channels = [
            guild.get_channel(channel_id) for channel_id in rooms.team_voice_ids
        ]
        service = _match_room_service_for_guild(guild)
        if any(getattr(channel, "members", ()) for channel in voice_channels):
            await service.mark_occupied(rooms.lobby_id)
        else:
            await service.mark_empty(rooms.lobby_id)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return
    if not message.content.startswith("."):
        # Ordinary chat: the only GodForge behavior here is optional passive
        # 67 reactions. Everything else requires the dot-command prefix.
        await _handle_r67_passive(message)
        return

    # ---- Feature-registered exact-token commands (see utils/routing) ----
    _first = message.content[1:].split()[0].lower() if message.content[1:].split() else ""
    _command_handler = command_registry.resolve(_first)
    if _command_handler is not None:
        await _command_handler(message)
        return

    intent = parser.parse(message.content)
    if intent is None:
        handled = await _handle_custom_command(message, _first)
        if handled:
            return
        return

    channel_id = message.channel.id

    try:
        if intent["kind"] == "help":
            await message.channel.send(embed=formatter.format_help_page1(), view=HelpView())
            return

        elif intent["kind"] == "session":
            async with sessions.get_lock(channel_id):
                response = await _handle_session(intent, channel_id)

        elif intent["kind"] == "draft":
            response = await _handle_draft(intent, message)
            if response is None:
                return

        elif intent["kind"] == "draft_action":
            response = await _handle_draft_action(intent, message)
            if response is None:
                return

        elif intent["kind"] == "god":
            async with sessions.get_lock(channel_id):
                session = sessions.get(channel_id)
                exclude = session.get_excluded_gods() if session else None
                god = picker.pick_god(loader.gods(), intent["role"], intent["source"],
                                      exclude=exclude)
                if session:
                    embed = formatter.format_rg_session(god, intent["role"], intent["source"])
                    sent = await message.channel.send(embed=embed)
                    session.register_rg(sent.id, god, intent["role"], intent["source"])
                    _tracked_messages[sent.id] = {
                        "kind": "rg", "god": god, "channel_id": channel_id,
                        "role": intent["role"], "source": intent["source"],
                        "author_id": message.author.id,
                        "author_name": message.author.display_name,
                    }
                    await sent.add_reaction("✅")
                    await sent.add_reaction("❌")
                    return
                else:
                    response = formatter.format_god(god, intent["role"], intent["source"])

        elif intent["kind"] == "roll5":
            async with sessions.get_lock(channel_id):
                session = sessions.get(channel_id)
                exclude = session.get_excluded_gods() if session else None
                gods = picker.pick_team(loader.gods(), intent["role"], intent["source"],
                                        exclude=exclude)
                if session:
                    embed = formatter.format_roll5_session(gods, intent["role"], intent["source"])
                    sent = await message.channel.send(embed=embed)
                    session.register_roll5(sent.id, gods)
                    _tracked_messages[sent.id] = {
                        "kind": "roll5", "gods": gods, "channel_id": channel_id,
                        "role": intent["role"], "source": intent["source"],
                        "author_id": message.author.id,
                        "author_name": message.author.display_name,
                    }
                    for emoji in NUMBER_EMOJIS:
                        await sent.add_reaction(emoji)
                    return
                else:
                    response = formatter.format_team(gods, intent["role"], intent["source"])

        elif intent["kind"] == "build":
            items = picker.pick_build(
                loader.builds(), intent["role"], intent["type"], intent["count"]
            )
            response = formatter.format_build(items, intent["role"], intent["type"])

        else:
            return

    except ValueError as e:
        response = formatter.format_error(str(e))
    except FileNotFoundError as e:
        log.error(f"Data file missing: {e}")
        response = formatter.format_error("Data file missing. Check bot logs.")
    except Exception as e:
        log.exception(f"Unexpected error handling '{message.content}'")
        response = formatter.format_error("Something went wrong. Check bot logs.")

    if isinstance(response, discord.Embed):
        await message.channel.send(embed=response)
    else:
        await message.channel.send(response)


# ── Session handlers ──────────────────────────────────────────────────────────

# `.session` command behavior is owned by the feature handler (Issue #48); bot.py
# injects the shared collaborators and keeps a thin delegator.
session_command_handler = SessionCommandHandler(
    sessions=sessions,
    formatter=formatter,
    tracked_messages=_tracked_messages,
    channel_has_active=_channel_has_active,
)


async def _handle_session(intent: dict, channel_id: int):
    return await session_command_handler.handle(intent, channel_id)


# ── Draft handlers ────────────────────────────────────────────────────────────

# The draft command handlers (local + activity) are owned by DraftCoordinator
# (Issue #48); bot.py keeps thin delegators.
async def _handle_draft(intent: dict, message: discord.Message):
    return await draft_coordinator.handle_draft(intent, message)


async def _handle_draft_action(intent: dict, message: discord.Message):
    return await draft_coordinator.handle_draft_action(intent, message)


# ── Local draft helpers (used when ACTIVITY_BACKEND_URL is not set) ───────────

# Draft board / claim-embed rendering is owned by DraftRenderer (Issue #48).
draft_renderer = DraftRenderer(
    formatter=formatter,
    tracked_messages=_tracked_messages,
    number_emojis=NUMBER_EMOJIS,
)


async def _update_draft_board(draft, channel):
    await draft_renderer.update_board(draft, channel)


async def _post_claim_embeds(draft, channel):
    await draft_renderer.post_claim_embeds(draft, channel)


async def _update_claim_embed(draft, team, channel):
    await draft_renderer.update_claim_embed(draft, team, channel)


# Draft feature coordinator (Issue #48): owns local + activity draft handlers,
# the activity-draft state, the WS listener, export posting, and claim reactions.
draft_coordinator = DraftCoordinator(
    client=client,
    drafts=drafts,
    activity_client=activity_client,
    renderer=draft_renderer,
    active_draft_store=active_draft_store,
    tracked_messages=_tracked_messages,
    formatter=formatter,
    resolve_god_name=resolve_god_name,
    reports_channels=REPORTS_CHANNELS,
    number_emojis=NUMBER_EMOJIS,
    channel_has_active=_channel_has_active,
    log=log,
)
feature_registry.register(DraftFeature(draft_coordinator))


# ── Reaction handler ──────────────────────────────────────────────────────────

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return
    message_id = payload.message_id
    if message_id not in _tracked_messages:
        return
    info = _tracked_messages[message_id]
    channel_id = info["channel_id"]
    emoji = str(payload.emoji)

    if info["kind"] in ("roll5", "rg"):
        await _handle_session_reaction(payload, info, message_id, channel_id, emoji)
    elif info["kind"] == "claim":
        await _handle_claim_reaction(payload, info, message_id, channel_id, emoji)


async def _handle_claim_reaction(payload, info, message_id, channel_id, emoji):
    await draft_coordinator.handle_claim_reaction(
        payload, info, message_id, channel_id, emoji
    )


async def _handle_session_reaction(payload, info, message_id, channel_id, emoji):
    async with sessions.get_lock(channel_id):
        session = sessions.get(channel_id)
        if not session:
            return
        if session.is_reaction_processed(message_id, emoji):
            return
        session.mark_reaction_processed(message_id, emoji)

        channel = client.get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _tracked_messages.pop(message_id, None)
            return

        author_id = info["author_id"]
        author_name = info["author_name"]

        if info["kind"] == "roll5" and emoji in NUMBER_EMOJIS:
            index = NUMBER_EMOJIS.index(emoji)
            god = session.lock_roll5_pick(message_id, index, author_id, author_name)
            if god:
                embed = formatter.format_roll5_locked(
                    info["gods"], index, author_name, info["role"], info["source"],
                )
                await msg.edit(embed=embed)
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
                _tracked_messages.pop(message_id, None)

        elif info["kind"] == "rg":
            if emoji == "✅":
                god = session.lock_rg_pick(message_id, author_id, author_name)
                if god:
                    embed = formatter.format_rg_locked(god, author_name, info["role"], info["source"])
                    await msg.edit(embed=embed)
                    try:
                        await msg.clear_reactions()
                    except discord.Forbidden:
                        pass
                    _tracked_messages.pop(message_id, None)
            elif emoji == "❌":
                god = session.discard_rg(message_id)
                if god:
                    embed = formatter.format_rg_discarded(god, info["role"], info["source"])
                    await msg.edit(embed=embed)
                    try:
                        await msg.clear_reactions()
                    except discord.Forbidden:
                        pass
                    _tracked_messages.pop(message_id, None)
                    log.info(f"Session discard: {god} discarded "
                             f"in channel {channel_id}")


# ── Legacy economy helpers ───────────────────────────────────────────────────

def _is_admin(message: discord.Message) -> bool:
    """True if the author is the bot owner or has server administrator permission."""
    if message.author.id == _GOD_USER_ID:
        return True
    member = message.author
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _can_manage_guild(message: discord.Message) -> bool:
    """True if the author may manage guild-level r67 settings.

    Discord's ``Manage Guild`` permission (or Administrator, which implies it)
    is required per Issue #47 (Gate 4); the bot owner is always allowed.
    """
    if getattr(message.author, "id", None) == _GOD_USER_ID:
        return True
    perms = getattr(message.author, "guild_permissions", None)
    return bool(perms and (getattr(perms, "manage_guild", False) or perms.administrator))


async def _handle_r67_command(message: discord.Message):
    """Thin adapter: parse the `.r67` command and send the service reply.

    All branching and business logic lives in ``R67Service``; this boundary only
    extracts Discord inputs and delivers the returned text.
    """
    # Split on arbitrary whitespace so ".r67\nstatus" or ".r67\tstatus" parse the
    # same as ".r67 status" (matches the whitespace-aware first-token detection).
    parts = message.content[1:].split(maxsplit=1)
    remainder = parts[1].strip().lower() if len(parts) > 1 else ""
    guild_id = message.guild.id if message.guild else None
    reply = r67_service.handle_command(
        guild_id,
        remainder,
        can_manage_guild=_can_manage_guild(message),
    )
    await message.channel.send(reply)


async def _handle_r67_passive(message: discord.Message):
    """Adapter for optional passive 67 reactions on ordinary guild messages.

    Passive reactions are opt-in per guild and skipped entirely in DMs or when
    the guild has disabled GodForge. All matching, cooldown, and roll logic lives
    in ``R67Service``; this boundary only guards eligibility and delivers text.
    """
    if message.guild is None:
        return
    guild_settings = settings.get_guild_settings(str(message.guild.id))
    if not guild_settings["features"].get("botEnabled", True):
        return
    try:
        outcome = r67_service.process_passive(
            message.guild.id,
            message.channel.id,
            message.author.id,
            message.content,
        )
    except Exception:
        log.exception("r67 passive handler failed for guild %s", message.guild.id)
        return

    if outcome.survivor_winners:
        await _run_r67_survivor_event(message, outcome.survivor_winners)
    if outcome.response:
        await message.channel.send(outcome.response)


async def _run_r67_survivor_event(message: discord.Message, winners: list[int]):
    """Grant the cosmetic 67 Survivor role and post the event announcement.

    The event cooldown is already committed by ``process_passive``; role failures
    never suppress the announcement (Gate 3). Removal is handled durably by the
    periodic cleanup task and startup recovery.
    """
    try:
        result = await r67_service.grant_survivor_roles(message.guild, winners)
        marked = result.marked
    except Exception:
        log.exception("67 Survivor role grant failed for guild %s", message.guild.id)
        marked = False
    log.info(
        "67 Survivor event fired in guild %s channel %s (marked=%s)",
        message.guild.id,
        message.channel.id,
        marked,
    )
    announcement = build_survivor_announcement(winners, marked)
    try:
        await message.channel.send(
            announcement,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not post 67 Survivor announcement in guild %s", message.guild.id)


async def _handle_deprecated_economy_command(message: discord.Message, command: str):
    await message.channel.send(
        f"⚠️ `.{command}` is deprecated in GodForge. "
        "Economy, wallets, ledgers, and betting are not available in standalone "
        "GodForge. Use GodForge for parties, randomizers, sessions, and drafts."
    )


async def _deprecated_economy_entry(message: discord.Message):
    """Registry entry point for deprecated economy tokens."""
    command = message.content[1:].split()[0].lower()
    await _handle_deprecated_economy_command(message, command)


# Feature-owned command registration (Issue #48). Each feature declares the exact
# dot-command tokens it owns; on_message resolves them through command_registry.
command_registry.register(("match", "bet", "wallet", "ledger"), _deprecated_economy_entry)
command_registry.register(("r67",), _handle_r67_command)


# Custom-command execution is owned by the feature runtime (Issue #48). bot.py
# constructs it with the shared admin check and keeps thin delegators so the
# adapter boundary (and existing tests) stay stable.
custom_command_runtime = CustomCommandRuntime(is_admin=_is_admin)
# Backward-compatible alias for the per-user cooldown map (same object).
_custom_command_cooldowns = custom_command_runtime.cooldowns


async def _handle_custom_command(message: discord.Message, trigger: str) -> bool:
    """Execute a dashboard-configured custom command if one matches."""
    return await custom_command_runtime.handle(message, trigger)


class HelpView(discord.ui.View):
    """Two-page paginated help embed (60 s timeout — no persistence needed)."""

    def __init__(self, page: int = 0):
        super().__init__(timeout=60)
        self._page = page

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._page = max(0, self._page - 1)
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._page = min(1, self._page + 1)
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    def _current_embed(self) -> discord.Embed:
        return formatter.format_help_page1() if self._page == 0 else formatter.format_help_page2()


# The /scrim command family and ScrimChallengeView are owned by the scrim
# feature module (Issue #48); bot.py wires it with injected dependencies.
_scrim_deps = ScrimCommandDeps(
    scrim_repository=scrim_repository,
    schedule_repository=schedule_repository,
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    ready_check_embed=lambda lobby_id, queue: _ready_check_embed(lobby_id, queue),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    lobby_card_embed=lambda lobby: _lobby_card_embed(lobby),
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
)
scrim_commands, ScrimChallengeView = register_scrim_commands(client.tree, _scrim_deps)
scrim_team_create = scrim_commands.team_create
scrim_teams = scrim_commands.teams
scrim_challenge = scrim_commands.challenge
scrim_respond = scrim_commands.respond
scrim_checkin = scrim_commands.checkin
scrim_lock = scrim_commands.lock
scrim_launch = scrim_commands.launch

party_commands = app_commands.Group(
    name="party",
    description="Set up and manage GodForge parties",
)
client.tree.add_command(party_commands)


# Per-guild temporary-room service construction is owned by the factory
# (Issue #48); bot.py keeps a thin delegator.
match_room_service_factory = MatchRoomServiceFactory(
    match_room_repository, settings.get_guild_settings
)


def _match_room_service_for_guild(guild: discord.Guild) -> MatchRoomService:
    return match_room_service_factory.for_guild(guild)


feature_registry.register(
    RoomLifecycle(match_room_repository, party_repository, match_room_service_factory)
)


# /party setup is owned by the party-setup-command feature module (Issue #48);
# bot.py wires it with injected dependencies.
_party_setup_deps = PartySetupCommandDeps(
    settings_module=settings,
    play_panel_view=lambda: PlayPanelView(_handle_play_panel_action),
)
register_party_setup_command(party_commands, _party_setup_deps)
party_setup = party_commands.setup


# /party room is owned by the party-room-command feature module (Issue #48);
# bot.py wires it with injected dependencies.
_party_room_deps = PartyRoomCommandDeps(
    party_repository=party_repository,
    match_room_service_for_guild=_match_room_service_for_guild,
    refresh_public_lobby_card=lambda lobby, guild: (
        party_lobby_service.refresh_public_lobby_card(lobby, guild)
    ),
)
register_party_room_command(party_commands, _party_room_deps)
party_room = party_commands.room
party_report = party_commands.report


# Scheduled-night /party subcommands are owned by the schedule feature module
# (Issue #48); bot.py wires it with injected dependencies.
_schedule_deps = ScheduleCommandDeps(
    schedule_repository=schedule_repository,
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    ready_check_embed=lambda lobby_id, queue: _ready_check_embed(lobby_id, queue),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    lobby_card_embed=lambda lobby: _lobby_card_embed(lobby),
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
)
register_schedule_commands(party_commands, _schedule_deps)
party_schedule = party_commands.schedule
party_confirm = party_commands.confirm
party_rsvp = party_commands.rsvp
party_unrsvp = party_commands.unrsvp
party_events = party_commands.events
party_calendar = party_commands.calendar
party_open_scheduled = party_commands.open_scheduled




async def _handle_play_panel_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_play_panel_action(interaction, action)


async def _handle_create_lobby_submission(
    interaction: discord.Interaction, payload: dict[str, object]
) -> None:
    await party_lobby_service.handle_create_lobby_submission(interaction, payload)


async def _join_lobby_from_preferences(
    interaction: discord.Interaction, lobby_id: str, payload: dict[str, object]
) -> None:
    await party_lobby_service.join_lobby_from_preferences(interaction, lobby_id, payload)


async def _ensure_party_queue(lobby):
    return await party_lobby_service.ensure_party_queue(lobby)


def _lobby_card_embed(lobby) -> discord.Embed:
    return party_lobby_service.lobby_card_embed(lobby)


def _lobby_id_from_interaction(interaction: discord.Interaction) -> str:
    return party_lobby_service.lobby_id_from_interaction(interaction)


async def _handle_lobby_card_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_lobby_card_action(interaction, action)


async def _launch_party_draft(
    interaction: discord.Interaction,
    lobby,
    *,
    formation_mode: FormationMode = FormationMode.ROLE_FIT,
) -> None:
    await party_lobby_service.launch_party_draft(
        interaction, lobby, formation_mode=formation_mode
    )

# Match-result rendering and history creation are owned by utils/match_results
# (Issue #48); bot.py keeps thin delegators.
def _ensure_match_history(lobby, launch):
    return match_results.ensure_match_history(match_history_repository, lobby, launch)


def _match_result_embed(record) -> discord.Embed:
    return match_results.build_result_embed(record)


def _match_id_from_interaction(interaction: discord.Interaction) -> str:
    return match_results.match_id_from_interaction(interaction)


# Match-result/continuity interaction handlers are owned by the match_actions
# feature module (Issue #48); bot.py wires it with injected dependencies.
_match_action_deps = MatchActionDeps(
    match_history_repository=match_history_repository,
    match_continuity_repository=match_continuity_repository,
    party_draft_repository=party_draft_repository,
    match_room_repository=match_room_repository,
    party_queue_service=party_queue_service,
    match_room_service_for_guild=_match_room_service_for_guild,
    match_result_view=lambda: MatchResultView(_handle_match_result_action),
    match_continuity_view=lambda allow_continue_series=True: MatchContinuityView(
        _handle_match_continuity_action, allow_continue_series=allow_continue_series
    ),
)


async def _handle_match_result_action(interaction: discord.Interaction, action: str) -> None:
    await handle_match_result_action(_match_action_deps, interaction, action)


async def _handle_match_continuity_action(
    interaction: discord.Interaction, action: str
) -> None:
    await handle_match_continuity_action(_match_action_deps, interaction, action)


# Play panel, lobby card, queue, and draft-launch orchestration are owned
# by PartyLobbyService (Issue #48); bot.py wires it with injected
# dependencies and keeps thin delegators for existing call sites.
_party_lobby_deps = PartyLobbyDeps(
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    party_draft_repository=party_draft_repository,
    scrim_repository=scrim_repository,
    drafts=drafts,
    draft_coordinator=draft_coordinator,
    activity_client=activity_client,
    formatter=formatter,
    settings_module=settings,
    reserve_match_id=match_ids.reserve_match_id,
    match_room_service_for_guild=_match_room_service_for_guild,
    channel_has_active=_channel_has_active,
    save_active_draft=_save_active_draft,
    ensure_match_history=_ensure_match_history,
    match_result_embed=_match_result_embed,
    set_member_role=set_member_role,
    log=log,
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    role_preferences_view=lambda: RolePreferencesView(_handle_role_preference),
    create_lobby_view=CreateLobbyWizardView,
    join_preferences_view=JoinPreferencesView,
    match_result_view=lambda: MatchResultView(_handle_match_result_action),
    match_formation_view=lambda: MatchFormationView(_handle_lobby_card_action),
    # Issue #63 additions — small ephemeral/organizer-only views. Each
    # factory mirrors the create_lobby_view/join_preferences_view convention
    # above: the caller supplies its own per-interaction handler closure, so
    # these views stay generic and don't need their own bot.py-level
    # persistent handler.
    recruiting_card_view=lambda: RecruitingCardView(_handle_lobby_card_action),
    queue_settings_view=lambda: QueueSettingsView(_handle_lobby_card_action),
    transfer_organizer_view=lambda handler, participants: TransferOrganizerView(
        handler, participants
    ),
    queue_select_view=lambda handler, queues: QueueSelectView(handler, queues),
    change_roles_view=lambda handler: ChangeRolesPromptView(handler),
    already_in_queue_view=lambda handler: AlreadyInQueueView(handler),
    rename_modal=lambda handler, current_name: RenameQueueModal(handler, current_name),
    start_queue_modal=lambda handler: StartQueueModal(handler),
)
party_lobby_service = PartyLobbyService(_party_lobby_deps)
feature_registry.register(PartyLobbyFeature(party_lobby_service))

def _reconcile_active_party_draft(lobby, launch, actor_id: int) -> None:
    party_lobby_service.reconcile_active_party_draft(lobby, launch, actor_id)


def _ready_check_embed(lobby_id: str, queue) -> discord.Embed:
    return party_lobby_service.ready_check_embed(lobby_id, queue)


async def _handle_ready_check_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_ready_check_action(interaction, action)


async def _handle_role_preference(interaction: discord.Interaction, role_key: str) -> None:
    await party_lobby_service.handle_role_preference(interaction, role_key)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
