import pytest
from ctools.log import configure_logging


@pytest.fixture(autouse=True, scope="session")
def _setup_logging():
    configure_logging(verbose=True)
