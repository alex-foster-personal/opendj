"""INSTALL-14213: desktop loopback health probe contract (issue #13305).

[if] loopback health checks regress [then] desktop shell adoption fails closed, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_RS = REPO_ROOT / "apps/desktop/src-tauri/src/engine.rs"
LAUNCH_RS = REPO_ROOT / "apps/desktop/src-tauri/src/launch.rs"

pytestmark = pytest.mark.requirement("INSTALL-14213")


def _health_ok_body(engine_rs: str) -> str:
    return engine_rs.split("pub fn health_ok", 1)[1].split("\npub fn ", 1)[0]


@pytest.mark.requirement("INSTALL-14213")
def test_health_ok_uses_raw_tcp_on_loopback() -> None:
    """[if] health_ok used an HTTP client [then] proxy env could block loopback, [else stop]."""
    health = _health_ok_body(ENGINE_RS.read_text(encoding="utf-8"))
    assert "TcpStream::connect_timeout" in health
    assert "127, 0, 0, 1" in health
    assert "read_to_end" not in health


@pytest.mark.requirement("INSTALL-14213")
def test_health_ok_stops_after_the_status_line() -> None:
    """[if] health_ok waits for EOF [then] a slow server times out, [else stop]."""
    health = _health_ok_body(ENGINE_RS.read_text(encoding="utf-8"))
    assert "byte[0] == b'\\n'" in health
    assert 'starts_with("HTTP/1.1 200")' in health


@pytest.mark.requirement("INSTALL-14213")
def test_missing_shell_log_parent_skips_without_shell_log_failed() -> None:
    """[if] NotFound logs [shell log failed] [then] panic-hook noise masks errors, [else stop]."""
    engine_rs = ENGINE_RS.read_text(encoding="utf-8")
    arm = engine_rs.split("fn append_shell_log_to_path", 1)[1]
    not_found = arm.split("ErrorKind::NotFound", 1)[1].split("}", 1)[0]
    assert "[shell log failed]" not in not_found


@pytest.mark.requirement("INSTALL-14213")
def test_launch_and_engine_tests_cover_health_probe_acceptance() -> None:
    """[if] Rust health tests vanish [then] the probe contract is unenforced, [else stop]."""
    launch_rs = LAUNCH_RS.read_text(encoding="utf-8")
    tests = launch_rs.split("#[cfg(test)]", 1)[1]
    engine_rs = ENGINE_RS.read_text(encoding="utf-8")
    assert "fn set_dead_proxy_env()" in tests
    assert "fn health_ok_true_for_local_200_server()" in tests
    assert "fn wait_health_ok(" in tests
    assert "fn held_live_healthy_lock_means_adopt()" in tests
    assert "fn shell_logging_missing_path_does_not_panic()" in engine_rs
