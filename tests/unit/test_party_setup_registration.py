import bot

from utils.setup_views import PlayPanelView, RolePreferencesView


def test_party_setup_slash_command_is_registered():
    party = bot.client.tree.get_command("party")

    assert party is not None
    assert party.get_command("setup") is not None


def test_setup_views_use_persistent_custom_ids():
    play = PlayPanelView(bot._handle_play_panel_action)
    roles = RolePreferencesView(bot._handle_role_preference)

    assert play.timeout is None
    assert roles.timeout is None
    assert len({item.custom_id for item in play.children + roles.children}) == 11
