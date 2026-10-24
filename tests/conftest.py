"""Keep tests off the network: Home Assistant calls fail as 'unreachable' unless a test stubs them."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _no_home_assistant_network(monkeypatch):
    import ha

    def offline(*_a, **_k):
        raise ha.HAError("Can't reach Home Assistant (offline in tests)", 1)

    monkeypatch.setattr(ha, "_open", offline)
    monkeypatch.setattr(ha, "ws_commands", offline)
    ha.clear_cache()
