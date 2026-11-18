import pytest

from cliphunter.jsonlog import set_level


@pytest.fixture(autouse=True)
def _quiet_logs():
    set_level("WARNING")
    yield
    set_level("INFO")
