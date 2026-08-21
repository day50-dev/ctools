import pytest

from ctools.agents import REGISTRY
from ctools.log import configure_logging


@pytest.fixture(autouse=True, scope="session")
def _setup_logging():
    configure_logging(verbose=True)


@pytest.fixture(autouse=True, scope="session")
def _isolate_agents(tmp_path_factory):
    """Point every agent at an empty directory for the whole test session.

    Without this, any command that sweeps all agents (cdir -R, the MCP
    search tool) reads the developer's real ~/.claude, ~/.codex and so on,
    which makes results depend on the machine it runs on. Tests that need
    real data patch the one agent they care about on top of this.
    """
    empty = tmp_path_factory.mktemp("no-agents")
    originals = {name: agent.base_path for name, agent in REGISTRY.items()}
    for agent in REGISTRY.values():
        agent.base_path = empty / agent.name
    yield
    for name, base_path in originals.items():
        REGISTRY[name].base_path = base_path
