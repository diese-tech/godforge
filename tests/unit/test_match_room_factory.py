"""MatchRoomServiceFactory wiring (Issue #48, Phase 5)."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from utils.match_room_factory import MatchRoomServiceFactory


def _settings(category="10", archive="20", test_mode=False):
    return lambda gid: {
        "managed": {
            "roomCategoryId": category,
            "playChannelId": archive,
            "testMode": test_mode,
        }
    }


def test_requires_setup_when_category_missing():
    factory = MatchRoomServiceFactory(MagicMock(), _settings(category="0"))
    with pytest.raises(RuntimeError, match="party setup"):
        factory.for_guild(MagicMock(id=1))


def test_archive_channel_is_not_required_for_room_cleanup():
    factory = MatchRoomServiceFactory(MagicMock(), _settings(archive="0"))

    assert factory.for_guild(MagicMock(id=1)) is not None


def test_builds_service_with_default_grace():
    factory = MatchRoomServiceFactory(MagicMock(), _settings())
    service = factory.for_guild(MagicMock(id=1))
    assert service.empty_grace == timedelta(minutes=10)


def test_test_mode_uses_short_grace():
    factory = MatchRoomServiceFactory(MagicMock(), _settings(test_mode=True))
    service = factory.for_guild(MagicMock(id=1))
    assert service.empty_grace == timedelta(minutes=1)
