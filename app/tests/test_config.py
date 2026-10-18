import pytest
from pydantic import ValidationError

from rag.config import Settings


def test_daily_confluence_sync_time():
    assert Settings(_env_file=None).confluence_daily_sync == (16, 0)
    assert Settings(_env_file=None, confluence_sync_time="7:05").confluence_daily_sync == (7, 5)
    assert Settings(_env_file=None, confluence_sync_time="").confluence_daily_sync is None


@pytest.mark.parametrize("value", ["24:00", "16", "4pm", "16:60"])
def test_invalid_sync_time_is_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, confluence_sync_time=value)


def test_confluence_is_off_by_default():
    s = Settings(_env_file=None)
    assert s.confluence_enabled is False
    assert s.confluence_space_keys == []
