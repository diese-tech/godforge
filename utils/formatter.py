"""
Format picker results as Discord messages.

`format_god` returns a discord.Embed (rich card with thumbnail + color).
`format_build` and `format_error` return plaintext strings.
The bot.py handler checks the return type and sends accordingly.
"""

import re
import discord

from utils.forgelens_adapter import forgelens_enabled


GODFORGE_VERSION = "2.3.0-rc.2"
RELEASE_NOTES = "Release notes: VERSION_HISTORY.md"

# Buff-themed role colors (Smite 2 has no green buff; support takes green).
ROLE_COLORS = {
    "jungle": 0xF1C40F,   # yellow
    "mid": 0xE74C3C,      # red
    "adc": 0x9B59B6,      # purple
    "support": 0x2ECC71,  # green
    "solo": 0x3498DB,     # blue
}
RANDOM_COLOR = 0xFFFFFF   # white - no specific role

# SmiteFire CDN base for god portrait icons.
ICON_BASE = "https://www.smitefire.com/images/v2/god/icon"


def god_slug(name: str) -> str:
    """
    Convert a god name to its SmiteFire CDN slug.
    'Loki' -> 'loki', 'Da Ji' -> 'da-ji', 'Morgan Le Fay' -> 'morgan-le-fay'
    Apostrophes are stripped defensively (current Smite 2 roster has none).
    """
    s = name.lower().replace("'", "").replace("’", "")
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _command_string(role: str | None, source: str, prefix: str = "rg") -> str:
    """Reconstruct the command the user (most likely) typed, for footer display."""
    if role is None:
        return f".{prefix}"
    role_char = {"jungle": "j", "mid": "m", "adc": "a", "support": "s", "solo": "o"}[role]
    source_char = "w" if source == "website" else "t"
    return f".{prefix}{role_char}{source_char}"


def format_god(god: str, role: str | None, source: str) -> discord.Embed:
    """Return a discord.Embed for a god pick."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    embed = discord.Embed(title=god, color=color)
    embed.set_thumbnail(url=f"{ICON_BASE}/{god_slug(god)}.png")
    embed.set_footer(text=f"{role_label} • {_command_string(role, source)}")
    return embed


def format_team(gods: list[str], role: str | None, source: str) -> discord.Embed:
    """Return a discord.Embed showing 5 gods in one card (non-session)."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    description = "\n".join(f"{i}. **{g}**" for i, g in enumerate(gods, 1))
    embed = discord.Embed(title=f"\U0001f3b2 {role_label} Roll5", description=description, color=color)
    embed.set_footer(text=f"{role_label} • {_command_string(role, source, prefix='roll5')}")
    return embed


# Number emoji constants for roll5 reactions.
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def format_roll5_session(gods: list[str], role: str | None, source: str) -> discord.Embed:
    """Roll5 embed for active session — numbered list, awaiting reaction pick."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    lines = [f"{NUMBER_EMOJIS[i]} **{g}**" for i, g in enumerate(gods)]
    description = "\n".join(lines)
    embed = discord.Embed(
        title=f"\U0001f3b2 {role_label} Roll5 — Pick one!",
        description=description,
        color=color,
    )
    embed.set_footer(text=f"{role_label} • {_command_string(role, source, prefix='roll5')} • React to select")
    return embed


def format_roll5_locked(gods: list[str], selected_index: int,
                        user_name: str, role: str | None, source: str) -> discord.Embed:
    """Edited roll5 embed showing which god was picked and by whom."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    lines = []
    for i, g in enumerate(gods):
        if i == selected_index:
            lines.append(f"\U0001f512 **{g}** ← {user_name}")
        else:
            lines.append(f"~~{g}~~")
    description = "\n".join(lines)
    embed = discord.Embed(
        title=f"\U0001f512 {role_label} Roll5 — Locked",
        description=description,
        color=color,
    )
    embed.set_footer(text=f"{role_label} • {_command_string(role, source, prefix='roll5')}")
    return embed


def format_rg_session(god: str, role: str | None, source: str) -> discord.Embed:
    """Single god embed for active session — awaiting ✅/❌ confirmation."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    embed = discord.Embed(title=god, color=color)
    embed.set_thumbnail(url=f"{ICON_BASE}/{god_slug(god)}.png")
    embed.set_footer(text=f"{role_label} • {_command_string(role, source)} • ✅ to lock, ❌ to discard")
    return embed


def format_rg_locked(god: str, user_name: str, role: str | None, source: str) -> discord.Embed:
    """Edited rg embed showing the god was locked by a user."""
    if role is None:
        color = RANDOM_COLOR
        role_label = "Random"
    else:
        color = ROLE_COLORS[role]
        role_label = role.capitalize()

    embed = discord.Embed(title=f"\U0001f512 {god}", description=f"Selected by **{user_name}**", color=color)
    embed.set_thumbnail(url=f"{ICON_BASE}/{god_slug(god)}.png")
    embed.set_footer(text=f"{role_label} • {_command_string(role, source)}")
    return embed


def format_rg_discarded(god: str, role: str | None, source: str) -> discord.Embed:
    """Edited rg embed showing the god was discarded."""
    embed = discord.Embed(title=f"❌ ~~{god}~~", description="Discarded", color=0x95A5A6)
    embed.set_footer(text=f"{_command_string(role, source)}")
    return embed


def format_session_show(picks: dict) -> discord.Embed:
    """Show all picks in the current session."""
    if not picks:
        embed = discord.Embed(
            title="\U0001f4cb Session Picks",
            description="No gods picked yet.",
            color=0x95A5A6,
        )
        return embed

    lines = [f"**{god}** → {info['user_name']}" for god, info in picks.items()]
    embed = discord.Embed(
        title=f"\U0001f4cb Session Picks ({len(picks)} total)",
        description="\n".join(lines),
        color=0x3498DB,
    )
    return embed


def format_session_end(picks: dict) -> discord.Embed:
    """Final locked summary when session ends."""
    if not picks:
        embed = discord.Embed(
            title="\U0001f4cb Session Complete",
            description="No gods were picked this session.",
            color=0x95A5A6,
        )
        return embed

    lines = [f"\U0001f512 **{god}** → {info['user_name']}" for god, info in picks.items()]
    embed = discord.Embed(
        title=f"\U0001f4cb Session Complete — {len(picks)} picks",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    return embed


def format_build(items: list[str], role: str, build_type: str | None) -> str:
    if role == "chaos":
        header = "\U0001f3b2 Chaos build"
    elif role == "support":
        header = "\U0001f6e1️ Support build"
    elif role == "adc":
        type_labels = {"standard": "standard", "str": "strength", "hyb": "hybrid"}
        header = f"\U0001f3f9 ADC build ({type_labels.get(build_type, build_type)})"
    else:
        type_labels = {"int": "intelligence", "str": "strength", "hyb": "hybrid"}
        header = f"⚔️ {role.capitalize()} build ({type_labels.get(build_type, build_type)})"

    numbered = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
    return f"{header}:\n{numbered}"


def format_error(message: str) -> str:
    return f"⚠️ {message}"


def format_help_page1() -> discord.Embed:
    """Page 1 of the help embed: gods, builds, sessions, draft."""
    embed = discord.Embed(title="GodForge Commands", color=0x3498DB)

    embed.add_field(name="Gods", value=(
        "`.rg` — random god from full roster\n"
        "`.rgj/m/a/s/o` — random god by role\n"
        "`.rgjt` / `.rgjw` — tab or website pool\n"
        "`.roll5` — 5 random gods from full roster\n"
        "`.roll5j` — 5 random gods of a role\n"
        "_Same role/source suffixes apply to both._"
    ), inline=False)

    embed.add_field(name="Builds  (append 1–5 for fewer items)", value=(
        "`.midint` / `.midstr`\n"
        "`.jungint` / `.jungstr`\n"
        "`.soloint` / `.solostr` / `.solohyb`\n"
        "`.adc` / `.adcstr` / `.adchyb`\n"
        "`.sup` — support\n"
        "`.rc` — chaos (reviewed item pool)"
    ), inline=False)

    embed.add_field(name="Sessions  (track picks, prevent duplicates)", value=(
        "`.session start` — start a session in this channel\n"
        "`.session show` — show picks so far\n"
        "`.session reset` — clear picks, keep session active\n"
        "`.session end` — end session, show summary\n"
        "_During a session, `.rg` and `.roll5` get reactions to lock picks._"
    ), inline=False)

    embed.add_field(name="Draft  (fearless competitive)", value=(
        "`.draft start @blue @red` — start a draft set\n"
        "`.draft show` — history + fearless pool\n"
        "`.draft next` — advance to next game\n"
        "`.draft undo` — undo last ban, pick, or advance\n"
        "`.draft end` — end set, export JSON\n"
        "`.ban GodName` / `.pick GodName` — your turn action\n"
        "_Aliases work: `.ban mlf`, `.pick baron`_"
    ), inline=False)

    embed.set_footer(text=f"Page 1/2 — GodForge v{GODFORGE_VERSION} • use ➡️ for usage tips • {RELEASE_NOTES}")
    return embed


def format_help_page2() -> discord.Embed:
    """Page 2 of the help embed: standalone usage tips."""
    embed = discord.Embed(title="GodForge Commands — Usage Tips", color=0x3498DB)

    embed.add_field(name="Pick a role", value=(
        "Use `j`, `m`, `a`, `s`, or `o` after `.rg` and `.roll5` for "
        "jungle, mid, ADC, support, or solo."
    ), inline=False)
    embed.add_field(name="Pick a source", value=(
        "Add `w` for the curated website pool or `t` for the in-game tab pool.\n"
        "Example: `.rgjt` rolls a jungle god from the tab source."
    ), inline=False)
    embed.add_field(name="Run a draft", value=(
        "Start with `.draft start @blue @red`, then captains use `.ban` and `.pick`.\n"
        "Use `.draft show`, `.draft undo`, `.draft next`, and `.draft end` to manage the set."
    ), inline=False)
    embed.add_field(name="Set up a server", value=(
        "Admins can run `/party setup` once to publish the persistent Play panel "
        "and create self-assignable role cosmetics."
    ), inline=False)

    embed.set_footer(text=f"Page 2/2 — GodForge v{GODFORGE_VERSION} • use ⬅️ for main commands • {RELEASE_NOTES}")
    return embed


def format_help() -> discord.Embed:
    """Return page 1 (default entry point for .help)."""
    return format_help_page1()


# ---- Draft formatting ----

BLUE_COLOR = 0x3498DB
RED_COLOR = 0xE74C3C
DRAFT_COLOR = 0x2C2F33  # dark gray for neutral draft embeds


def _pad_list(items: list[str], target: int, placeholder: str = "—") -> list[str]:
    """Pad a list to target length with placeholder strings."""
    return items + [placeholder] * (target - len(items))


def _forgelens_status_value(draft_status: str, draft_id: str, forgelens_match_id: str,
                            game_number: int, draft_sequence: int) -> str:
    return "\n".join([
        f"draft_status={draft_status}",
        f"draft_id={draft_id}",
        f"forgelens_match_id={forgelens_match_id}",
        f"game_number={game_number}",
        f"draft_sequence={draft_sequence}",
    ])


def _add_forgelens_status_field(embed: discord.Embed, draft_status: str, draft_id: str,
                                forgelens_match_id: str, game_number: int,
                                draft_sequence: int) -> None:
    if not forgelens_enabled():
        return
    embed.add_field(
        name="ForgeLens Status",
        value=_forgelens_status_value(
            draft_status, draft_id, forgelens_match_id, game_number, draft_sequence
        ),
        inline=False,
    )


def format_draft_board(draft) -> discord.Embed:
    """Living embed showing current game state."""
    from utils.draft import get_phase_label
    game = draft.current_game
    turn = game.current_turn()
    phase = get_phase_label(game.step)

    if turn:
        team, action = turn
        captain = draft.blue_captain["name"] if team == "blue" else draft.red_captain["name"]
        team_emoji = "\U0001f535" if team == "blue" else "\U0001f534"
        status = f"{team_emoji} **{captain}** — {action}"
    else:
        status = "✅ Game complete! Use `.draft next` or `.draft end`"

    title = f"\U0001f4cb Draft {draft.draft_id} — Game {game.game_number} — {phase}"
    embed = discord.Embed(title=title, color=DRAFT_COLOR)

    # Bans - side by side
    blue_bans = _pad_list(game.bans["blue"], 5)
    red_bans = _pad_list(game.bans["red"], 5)
    embed.add_field(name="\U0001f535 Blue Bans", value="\n".join(blue_bans), inline=True)
    embed.add_field(name="⠀", value="⠀", inline=True)  # spacer
    embed.add_field(name="\U0001f534 Red Bans", value="\n".join(red_bans), inline=True)

    # Picks - side by side
    blue_picks = _pad_list(game.picks["blue"], 5)
    red_picks = _pad_list(game.picks["red"], 5)
    embed.add_field(name="\U0001f535 Blue Picks", value="\n".join(blue_picks), inline=True)
    embed.add_field(name="⠀", value="⠀", inline=True)  # spacer
    embed.add_field(name="\U0001f534 Red Picks", value="\n".join(red_picks), inline=True)

    # Fearless pool
    if draft.fearless_pool:
        fearless = ", ".join(sorted(draft.fearless_pool))
        embed.add_field(name="\U0001f6ab Fearless Pool", value=fearless, inline=False)

    _add_forgelens_status_field(
        embed,
        draft.current_status(),
        draft.draft_id,
        getattr(draft, "forgelens_match_id", ""),
        game.game_number,
        getattr(draft, "draft_sequence", 1),
    )
    embed.add_field(name="⏳ Current Turn", value=status, inline=False)
    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft.draft_id}")
    return embed


def format_draft_show(draft) -> discord.Embed:
    """Full history embed — all games + fearless pool."""
    embed = discord.Embed(
        title=f"\U0001f4cb Draft {draft.draft_id} — Full History",
        color=DRAFT_COLOR,
    )

    # Captains
    embed.add_field(
        name="Captains",
        value=f"\U0001f535 {draft.blue_captain['name']}  vs  \U0001f534 {draft.red_captain['name']}",
        inline=False,
    )

    # Completed games
    for game in draft.completed_games:
        blue_picks = ", ".join(game.picks["blue"]) or "None"
        red_picks = ", ".join(game.picks["red"]) or "None"
        blue_bans = ", ".join(game.bans["blue"]) or "None"
        red_bans = ", ".join(game.bans["red"]) or "None"
        embed.add_field(
            name=f"Game {game.game_number}",
            value=(
                f"\U0001f535 Picks: {blue_picks}\n"
                f"\U0001f534 Picks: {red_picks}\n"
                f"\U0001f535 Bans: {blue_bans}\n"
                f"\U0001f534 Bans: {red_bans}"
            ),
            inline=False,
        )

    # Current game (if it has activity)
    cg = draft.current_game
    if cg.step > 0:
        blue_picks = ", ".join(cg.picks["blue"]) or "None"
        red_picks = ", ".join(cg.picks["red"]) or "None"
        blue_bans = ", ".join(cg.bans["blue"]) or "None"
        red_bans = ", ".join(cg.bans["red"]) or "None"
        status = "✅ Complete" if cg.is_complete() else "\U0001f504 In Progress"
        embed.add_field(
            name=f"Game {cg.game_number} ({status})",
            value=(
                f"\U0001f535 Picks: {blue_picks}\n"
                f"\U0001f534 Picks: {red_picks}\n"
                f"\U0001f535 Bans: {blue_bans}\n"
                f"\U0001f534 Bans: {red_bans}"
            ),
            inline=False,
        )

    # Fearless pool
    if draft.fearless_pool:
        fearless = ", ".join(sorted(draft.fearless_pool))
    else:
        fearless = "None yet"
    embed.add_field(name="\U0001f6ab Fearless Pool", value=fearless, inline=False)
    _add_forgelens_status_field(
        embed,
        draft.current_status(),
        draft.draft_id,
        getattr(draft, "forgelens_match_id", ""),
        draft.current_game.game_number,
        getattr(draft, "draft_sequence", 1),
    )
    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft.draft_id}")
    return embed


def format_draft_end(draft, export: dict) -> discord.Embed:
    """Final summary when draft set ends."""
    embed = discord.Embed(
        title=f"\U0001f3c1 Draft {draft.draft_id} — Complete",
        color=0x2ECC71,
    )

    embed.add_field(
        name="Captains",
        value=f"\U0001f535 {draft.blue_captain['name']}  vs  \U0001f534 {draft.red_captain['name']}",
        inline=False,
    )

    for game_data in export["games"]:
        blue_picks = ", ".join(game_data["picks"]["blue"]) or "None"
        red_picks = ", ".join(game_data["picks"]["red"]) or "None"
        blue_bans = ", ".join(game_data["bans"]["blue"]) or "None"
        red_bans = ", ".join(game_data["bans"]["red"]) or "None"
        # Check if game was fully drafted (5 picks per side)
        is_complete = (len(game_data["picks"]["blue"]) == 5
                       and len(game_data["picks"]["red"]) == 5)
        status = "✅" if is_complete else "⚠️ Incomplete"
        embed.add_field(
            name=f"Game {game_data['game_number']} {status}",
            value=(
                f"\U0001f535 Picks: {blue_picks}\n"
                f"\U0001f534 Picks: {red_picks}\n"
                f"\U0001f535 Bans: {blue_bans}\n"
                f"\U0001f534 Bans: {red_bans}"
            ),
            inline=False,
        )

    if export["fearless_pool"]:
        fearless = ", ".join(export["fearless_pool"])
        embed.add_field(name="\U0001f6ab Fearless Pool", value=fearless, inline=False)

    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft.draft_id}")
    return embed


def format_draft_action(team: str, action_type: str, god: str, draft_id: str) -> str:
    """Short confirmation message after a ban or pick."""
    emoji = "\U0001f535" if team == "blue" else "\U0001f534"
    action_word = "banned" if action_type == "ban" else "picked"
    return f"{emoji} **{god}** {action_word} • {draft_id}"


def format_draft_undo(team: str, action_type: str, god: str) -> str:
    """Confirmation of an undo."""
    emoji = "\U0001f535" if team == "blue" else "\U0001f534"
    action_word = "ban" if action_type == "ban" else "pick"
    return f"↩️ Undid {emoji} {action_word} of **{god}**"


def format_draft_next(draft) -> str:
    """Confirmation of advancing to next game."""
    fearless = ", ".join(sorted(draft.fearless_pool))
    return (
        f"✅ Game {draft.current_game.game_number - 1} locked! "
        f"Starting **Game {draft.current_game.game_number}**.\n"
        f"\U0001f6ab Fearless pool: {fearless}"
    )


def format_claim_embed(team: str, picks: list[str], claims: dict,
                       draft_id: str, forgelens_match_id: str = "",
                       game_number: int = 1, draft_sequence: int = 1) -> discord.Embed:
    """
    Claim embed for one team. Players react 1️⃣-5️⃣ to claim their god.
    claims dict: god_name -> {"user_id": ..., "name": ..., ...} or missing if unclaimed.
    """
    color = 0x3498DB if team == "blue" else 0xE74C3C
    team_label = "\U0001f535 Blue" if team == "blue" else "\U0001f534 Red"

    lines = []
    all_claimed = True
    for i, god in enumerate(picks):
        if god in claims:
            lines.append(f"{NUMBER_EMOJIS[i]} **{god}** → {claims[god]['name']}")
        else:
            lines.append(f"{NUMBER_EMOJIS[i]} **{god}**")
            all_claimed = False

    description = "\n".join(lines)

    if all_claimed:
        title = f"{team_label} Team — All claimed! ✅"
    else:
        title = f"{team_label} Team — Claim your god!"

    embed = discord.Embed(title=title, description=description, color=color)
    _add_forgelens_status_field(
        embed,
        "draft_complete" if all_claimed else ("claiming" if claims else "picks_bans_complete"),
        draft_id,
        forgelens_match_id,
        game_number,
        draft_sequence,
    )
    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft_id} • React to claim")
    return embed


def format_claim_undo(team: str, god: str, user_name: str) -> str:
    """Confirmation of a claim undo."""
    emoji = "\U0001f535" if team == "blue" else "\U0001f534"
    return f"↩️ Undid {emoji} claim: **{user_name}** unclaimed **{god}**"


# ── Activity backend snapshot formatters ────────────────────────────────────────────────────────────────────────────────

def format_board_from_snapshot(snapshot: dict) -> discord.Embed:
    """Living embed built from an Activity StateSnapshot dict."""
    draft_id = snapshot.get("draftId", "?")
    game_number = snapshot.get("gameNumber", 1)
    phase = snapshot.get("phase", "")
    blue_captain = snapshot.get("blueCaptain", {}).get("name", "Blue")
    red_captain = snapshot.get("redCaptain", {}).get("name", "Red")

    turn = snapshot.get("currentTurn")
    if turn:
        team = turn.get("team", "blue")
        action = turn.get("action", "")
        captain_name = blue_captain if team == "blue" else red_captain
        team_emoji = "\U0001f535" if team == "blue" else "\U0001f534"
        status = f"{team_emoji} **{captain_name}** — {action}"
    else:
        status = "✅ Game complete!"

    title = f"\U0001f4cb Draft {draft_id} — Game {game_number} — {phase}"
    embed = discord.Embed(title=title, color=DRAFT_COLOR)

    bans = snapshot.get("bans", {"blue": [], "red": []})
    picks = snapshot.get("picks", {"blue": [], "red": []})
    blue_bans = _pad_list(bans.get("blue", []), 5)
    red_bans = _pad_list(bans.get("red", []), 5)
    embed.add_field(name="\U0001f535 Blue Bans", value="\n".join(blue_bans), inline=True)
    embed.add_field(name="⠀", value="⠀", inline=True)
    embed.add_field(name="\U0001f534 Red Bans", value="\n".join(red_bans), inline=True)

    blue_picks = _pad_list(picks.get("blue", []), 5)
    red_picks = _pad_list(picks.get("red", []), 5)
    embed.add_field(name="\U0001f535 Blue Picks", value="\n".join(blue_picks), inline=True)
    embed.add_field(name="⠀", value="⠀", inline=True)
    embed.add_field(name="\U0001f534 Red Picks", value="\n".join(red_picks), inline=True)

    fearless_pool = snapshot.get("fearlessPool", [])
    if fearless_pool:
        embed.add_field(name="\U0001f6ab Fearless Pool", value=", ".join(fearless_pool), inline=False)

    _add_forgelens_status_field(
        embed,
        snapshot.get("draftStatus", "drafting"),
        draft_id,
        snapshot.get("forgelensMatchId", ""),
        game_number,
        snapshot.get("draftSequence", 1),
    )
    embed.add_field(name="⏳ Current Turn", value=status, inline=False)
    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft_id}")
    return embed


def format_draft_end_from_export(export: dict) -> discord.Embed:
    """Final summary embed built from an Activity DraftExport dict."""
    draft_id = export.get("draftId") or export.get("draft_id", "?")
    blue_captain = export.get("blueCaptain", export.get("blue_captain", {})).get("name", "Blue")
    red_captain = export.get("redCaptain", export.get("red_captain", {})).get("name", "Red")

    embed = discord.Embed(
        title=f"\U0001f3c1 Draft {draft_id} — Complete",
        color=0x2ECC71,
    )
    embed.add_field(
        name="Captains",
        value=f"\U0001f535 {blue_captain} vs \U0001f534 {red_captain}",
        inline=False,
    )

    for game_data in export.get("games", []):
        bp = ", ".join(game_data["picks"]["blue"]) or "None"
        rp = ", ".join(game_data["picks"]["red"]) or "None"
        bb = ", ".join(game_data["bans"]["blue"]) or "None"
        rb = ", ".join(game_data["bans"]["red"]) or "None"
        is_complete = (len(game_data["picks"]["blue"]) == 5
                       and len(game_data["picks"]["red"]) == 5)
        status = "✅" if is_complete else "⚠️ Incomplete"
        embed.add_field(
            name=f"Game {game_data['game_number']} {status}",
            value=(
                f"\U0001f535 Picks: {bp}\n"
                f"\U0001f534 Picks: {rp}\n"
                f"\U0001f535 Bans: {bb}\n"
                f"\U0001f534 Bans: {rb}"
            ),
            inline=False,
        )

    fearless_pool = export.get("fearlessPool", [])
    if fearless_pool:
        embed.add_field(name="\U0001f6ab Fearless Pool", value=", ".join(fearless_pool), inline=False)

    _add_forgelens_status_field(
        embed,
        export.get("status", "draft_complete"),
        draft_id,
        export.get("forgelensMatchId", export.get("forgelens_match_id", "")),
        export.get("gameNumber", export.get("game_number", 1)),
        export.get("draftSequence", export.get("draft_sequence", 1)),
    )
    embed.set_footer(text=f"GodForge v{GODFORGE_VERSION} • Draft {draft_id}")
    return embed
