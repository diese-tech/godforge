from unittest.mock import AsyncMock, Mock

import pytest

from utils.setup_views import (
    PLAY_ACTIONS,
    PLAY_CUSTOM_ID_PREFIX,
    ROLE_CUSTOM_ID_PREFIX,
    ROLE_PREFERENCES,
    PlayPanelView,
    RolePreferencesView,
)


def _interaction(*, response_done: bool = False):
    interaction = Mock()
    interaction.response.is_done.return_value = response_done
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def test_play_panel_is_persistent_with_stable_custom_id():
    view = PlayPanelView(AsyncMock())

    assert view.timeout is None
    assert [button.custom_id for button in view.children] == [
        f"{PLAY_CUSTOM_ID_PREFIX}:{action_key}:v1"
        for action_key, _label, _style in PLAY_ACTIONS
    ]
    assert [button.label for button in view.children] == [
        "Start Queue",
        "Find a Queue",
        "My Roles",
    ]


@pytest.mark.asyncio
async def test_play_panel_delegates_to_injected_handler():
    handler = AsyncMock()
    view = PlayPanelView(handler)
    interaction = _interaction()

    await view.children[0].callback(interaction)

    handler.assert_awaited_once_with(interaction, "create")


def test_role_preferences_are_persistent_with_stable_custom_ids():
    view = RolePreferencesView(AsyncMock())

    assert view.timeout is None
    assert [button.custom_id for button in view.children] == [
        f"{ROLE_CUSTOM_ID_PREFIX}:{role_key}:v1"
        for role_key, _label, _style in ROLE_PREFERENCES
    ]
    assert [button.label for button in view.children] == [
        "Solo",
        "Jungle",
        "Mid",
        "Support",
        "ADC",
        "Captain",
        "Substitute",
        "LFG",
    ]


@pytest.mark.asyncio
async def test_role_preference_delegates_selected_role():
    handler = AsyncMock()
    view = RolePreferencesView(handler)
    interaction = _interaction()

    await view.children[2].callback(interaction)

    handler.assert_awaited_once_with(interaction, "mid")


@pytest.mark.asyncio
async def test_handler_failure_uses_initial_ephemeral_response():
    handler = AsyncMock(side_effect=RuntimeError("sensitive detail"))
    view = PlayPanelView(handler)
    interaction = _interaction()

    await view.children[0].callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "sensitive detail" not in args[0]
    assert kwargs == {"ephemeral": True}
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_failure_uses_ephemeral_followup_after_response():
    handler = AsyncMock(side_effect=RuntimeError("failure"))
    view = RolePreferencesView(handler)
    interaction = _interaction(response_done=True)

    await view.children[0].callback(interaction)

    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}
