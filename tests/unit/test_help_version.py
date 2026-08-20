from utils import formatter


def test_help_footer_uses_central_release_version():
    page1 = formatter.format_help_page1().to_dict()
    page2 = formatter.format_help_page2().to_dict()

    assert formatter.GODFORGE_VERSION == "2.3.0-rc.3"
    assert f"GodForge v{formatter.GODFORGE_VERSION}" in page1["footer"]["text"]
    assert f"GodForge v{formatter.GODFORGE_VERSION}" in page2["footer"]["text"]
    assert "VERSION_HISTORY.md" in page1["footer"]["text"]
    assert "VERSION_HISTORY.md" in page2["footer"]["text"]


def test_help_pages_only_describe_standalone_commands():
    help_text = str(formatter.format_help_page1().to_dict())
    help_text += str(formatter.format_help_page2().to_dict())

    assert "ForgeLens" not in help_text
    assert "economy" not in help_text.lower()
    assert ".bet" not in help_text
    assert ".wallet" not in help_text
    assert ".ledger" not in help_text
