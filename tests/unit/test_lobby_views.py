from unittest.mock import AsyncMock, Mock

import discord
import pytest

from utils.lobby_views import (
    LOBBY_CARD_ACTIONS,
    LOBBY_CARD_CUSTOM_ID_PREFIX,
    READY_CHECK_ACTIONS,
    READY_CHECK_CUSTOM_ID_PREFIX,
    CreateLobbyWizardView,
    JoinPreferencesView,
    LobbyCardView,
    MatchFormationView,
    MatchResultView,
    ReadyCheckView,
    _ValidationError,
)


def _interaction(response_done=False):
    interaction = Mock()
    interaction.response.is_done.return_value = response_done
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def test_create_wizard_uses_only_constrained_controls_for_configuration():
    view = CreateLobbyWizardView(AsyncMock())

    assert [item.custom_id for item in view.children] == [
        "godforge:lobby:create:mode:v2",
        "godforge:lobby:create:region:v2",
        "godforge:lobby:create:format:v2",
        "godforge:lobby:create:capacity:v2",
        "godforge:lobby:create:next:v2",
        "godforge:lobby:create:cancel:v2",
    ]
    capacity = view.children[3]
    assert [option.value for option in capacity.options] == ["2", "4", "6", "8", "10"]
    assert all(not isinstance(item, type(discord.ui.TextInput(label="x"))) for item in view.children)


def test_join_wizard_exposes_role_and_boolean_selections_without_text_inputs():
    # Issue #63: captain willingness is no longer collected during a normal
    # queue join — only Primary role, Secondary role, and Fill.
    view = JoinPreferencesView(AsyncMock())

    assert [item.custom_id for item in view.children] == [
        "godforge:lobby:join:primary:v2",
        "godforge:lobby:join:secondary:v2",
        "godforge:lobby:join:fill:v2",
        "godforge:lobby:join:confirm:v2",
        "godforge:lobby:join:cancel:v2",
    ]


def test_private_match_controls_are_persistent_and_include_room_recovery_actions():
    view = MatchFormationView(AsyncMock())

    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        "godforge:match:formation:teams_role_fit:v1",
        "godforge:match:formation:teams_balanced:v1",
        "godforge:match:formation:teams_captains:v1",
        "godforge:match:formation:move_teams_voice:v1",
        "godforge:match:formation:room_lock:v1",
        "godforge:match:formation:room_unlock:v1",
        "godforge:match:formation:room_close:v1",
    ]


@pytest.mark.asyncio
async def test_create_wizard_delegates_a_complete_constrained_payload_once():
    handler = AsyncMock()
    view = CreateLobbyWizardView(
        handler,
        initial={
            "mode": "conquest",
            "region": "na east",
            "format": "pug",
            "party_size": 6,
            "voice_required": True,
            "skill_band": "intermediate",
            "notes": "chill games",
            "queue_name": "Late Night Inhouses",
        },
    )
    interaction = _interaction()

    await view.act(interaction, "create")

    handler.assert_awaited_once_with(
        interaction,
        {
            "mode": "conquest",
            "region": "na east",
            "format": "pug",
            "party_size": 6,
            "voice_required": True,
            "skill_band": "intermediate",
            "notes": "chill games",
            "queue_name": "Late Night Inhouses",
        },
    )


@pytest.mark.asyncio
async def test_join_wizard_rejects_duplicate_roles_and_cancel_is_safe():
    handler = AsyncMock()
    view = JoinPreferencesView(
        handler,
        initial={
            "primary_role": "mid",
            "secondary_role": "mid",
            "fill": False,
        },
    )
    interaction = _interaction()

    await view.children[3].callback(interaction)

    handler.assert_not_awaited()
    assert "must be different" in interaction.response.send_message.await_args.args[0]

    cancelled = JoinPreferencesView(handler)
    cancel_interaction = _interaction()
    await cancelled.children[4].callback(cancel_interaction)
    cancel_interaction.response.edit_message.assert_awaited_once_with(
        content="Queue join cancelled.", view=None
    )


async def test_join_wizard_secondary_role_defaults_to_none_without_selection():
    # Issue #63 follow-up: Secondary role is described as optional, so Join
    # must succeed without the player ever touching that select — not just
    # without picking a specific role from it.
    view = JoinPreferencesView(AsyncMock())

    assert view.state["secondary_role"] is None
    secondary_select = next(
        item for item in view.children
        if getattr(item, "custom_id", "") == "godforge:lobby:join:secondary:v2"
    )
    assert next(o for o in secondary_select.options if o.default).value == "none"


async def test_join_wizard_requires_only_primary_role_and_fill():
    handler = AsyncMock()
    view = JoinPreferencesView(handler)
    interaction = _interaction()

    # Primary role missing entirely -> rejected.
    with pytest.raises(_ValidationError):
        await view.act(interaction, "confirm")
    handler.assert_not_awaited()

    # Primary role set, Fill still missing -> rejected.
    view.state["primary_role"] = "mid"
    with pytest.raises(_ValidationError):
        await view.act(interaction, "confirm")
    handler.assert_not_awaited()

    # Primary + Fill set, Secondary never touched -> succeeds using the
    # default None.
    view.state["fill"] = False
    await view.act(interaction, "confirm")
    handler.assert_awaited_once_with(
        interaction, {"primary_role": "mid", "secondary_role": None, "fill": False}
    )


async def test_join_wizard_secondary_role_still_selectable():
    view = JoinPreferencesView(AsyncMock())
    interaction = _interaction()

    await view.select(interaction, "secondary_role", "support")

    assert view.state["secondary_role"] == "support"


def test_lobby_card_is_persistent_with_stable_action_ids():
    view = LobbyCardView(AsyncMock())

    assert view.timeout is None
    assert len(view.children) == 9
    assert [item.custom_id for item in view.children] == [
        f"{LOBBY_CARD_CUSTOM_ID_PREFIX}:{action}:v1"
        for action, _label, _style in LOBBY_CARD_ACTIONS
    ]
    assert [item.label for item in view.children] == [
        "Join",
        "Leave",
        "Edit",
        "Cancel",
        "Share",
        "Ready Check",
        "Role Fit Teams",
        "Balanced Teams",
        "Captain Teams",
        ]
    assert [item.row for item in view.children] == [0, 0, 0, 0, 0, 1, 1, 1, 1]


@pytest.mark.asyncio
async def test_lobby_card_delegates_action_and_hides_handler_errors():
    handler = AsyncMock(side_effect=RuntimeError("private failure"))
    view = LobbyCardView(handler)
    interaction = _interaction(response_done=True)

    await view.children[4].callback(interaction)

    handler.assert_awaited_once_with(interaction, "share")
    message = interaction.followup.send.await_args.args[0]
    assert "private failure" not in message
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_lobby_ready_check_action_is_delegated():
    handler = AsyncMock()
    view = LobbyCardView(handler)
    interaction = _interaction()

    await view.children[5].callback(interaction)

    handler.assert_awaited_once_with(interaction, "ready_check")


def test_ready_check_view_is_persistent_with_stable_actions():
    view = ReadyCheckView(AsyncMock())

    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        f"{READY_CHECK_CUSTOM_ID_PREFIX}:{action}:v1"
        for action, _label, _style in READY_CHECK_ACTIONS
    ]
    assert [item.label for item in view.children] == [
        "Ready",
        "Need 5 Minutes",
        "Drop",
    ]
    assert all(item.row == 0 for item in view.children)


def test_match_result_view_is_persistent_with_stable_actions():
    view = MatchResultView(AsyncMock())

    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        "godforge:match:result:team_one:v1",
        "godforge:match:result:team_two:v1",
        "godforge:match:result:cancelled:v1",
        "godforge:match:result:no_contest:v1",
    ]
    assert all(item.row == 0 for item in view.children)


@pytest.mark.asyncio
async def test_ready_check_delegates_and_safely_reports_failures():
    handler = AsyncMock(side_effect=RuntimeError("private detail"))
    view = ReadyCheckView(handler)
    interaction = _interaction()

    await view.children[1].callback(interaction)

    handler.assert_awaited_once_with(interaction, "need_five")
    message = interaction.response.send_message.await_args.args[0]
    assert "private detail" not in message
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}
