"""Ledger origin resolution (AGENT-15, AGENT-16).

Separate module because the rest of the ledger suite monkeypatches
:func:`apps.fleet_mcp.ledger.base_url` away, which is exactly what let its
first version ship a call that would have raised ``TypeError`` the first time
an agent used the tool for real: ``resolve_backend_port`` takes ``explicit_port``
positionally, and nothing in the suite ever reached the call.
"""

from __future__ import annotations

import pytest

from apps.fleet_mcp import ledger
from apps.fleet_mcp.runner import UNKNOWN
from apps.webui import port_config


@pytest.mark.requirement("AGENT-15")
def test_base_url_uses_this_worktrees_backend_port(monkeypatch):
    """[if] this worktree has a claimed backend port [then] base_url names it, [else stop]"""
    monkeypatch.setenv(port_config.BACKEND_ENV, "8765")
    assert ledger.base_url() == "http://127.0.0.1:8765"


@pytest.mark.requirement("AGENT-16")
def test_an_unresolvable_port_is_unknown_with_a_remedy(monkeypatch):
    """[if] no backend port resolves [then] the failure is UNKNOWN with a remedy, [else stop]

    Not an exception the agent has to interpret, and not a guessed default
    port: a guessed port would make the next call fail somewhere less
    informative.
    """

    def refuse(*args, **kwargs):
        raise port_config.PortConfigError("no reservation for this worktree")

    monkeypatch.setattr(port_config, "resolve_backend_port", refuse)
    with pytest.raises(ledger.LedgerFailure) as caught:
        ledger.base_url()
    document = caught.value.document
    assert document["status"] == UNKNOWN
    assert "just webui-ports" in document["remedy"]
