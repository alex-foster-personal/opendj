"""Production application wiring contract for the existing Sets router."""
from __future__ import annotations

import pytest

from apps.webui.server.app import create_app


def test_create_app_registers_session_rec_http_contract() -> None:
    schema = create_app(mount_frontend=False).openapi()

    assert "/api/sets" in schema["paths"]
    assert "/api/sets/recorder" in schema["paths"]
    assert "/api/sets/recorder/start" in schema["paths"]
    assert "/api/sets/recorder/{session_id}/stop" in schema["paths"]
    assert "/api/sets/recorder/{session_id}/recover" in schema["paths"]
    assert "/api/sets/{session_id}/timeline" in schema["paths"]
    assert "/api/sets/{session_id}/audio/{segment}" in schema["paths"]

pytestmark = pytest.mark.rb_parity
