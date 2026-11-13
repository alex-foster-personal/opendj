"""The runtime-load scan over a staged payload: live, stale and unlisted sites (INSTALL-11).

The unit tests in test_engine_payload.py feed hand-built sites to the
classifier. These stage real files and run the whole scan, so each verdict is
the one ``scan_runtime_loaded_libraries`` gives a payload build:

- if a ``path:line`` key's line still holds its load site, the scan passes it.
- if a ``path:line`` key's file ships but the line moved, the scan fails as stale.
- if a torch load the allowlist does not name is discovered, the scan fails as
  unclassified, not as stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    LINE_KEY_PATTERN,
    RUNTIME_LOAD_ALLOWLIST,
    PayloadBuildError,
    scan_runtime_loaded_libraries,
)

OPS_PATH: str = "pylib/torch/_ops.py"


def _keyed_lines(relative: str) -> list[int]:
    """Every line the allowlist keys for one payload-relative file."""
    return sorted(
        int(match.group("line"))
        for key in RUNTIME_LOAD_ALLOWLIST
        if (match := LINE_KEY_PATTERN.match(key)) is not None and match.group("path") == relative
    )


def _ship(payload_dir: Path, relative: str, sites_at: list[int], length: int) -> None:
    """Stage one file with a dynamic CDLL call on each given line and comments elsewhere."""
    target = payload_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ctypes.CDLL(path)" if lineno in sites_at else "# no load site here" for lineno in range(1, length + 1)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ship_ops(payload_dir: Path, *, shift: int = 0) -> list[int]:
    """Stage torch/_ops.py with its keyed sites moved down by ``shift`` lines."""
    keyed = _keyed_lines(OPS_PATH)
    assert keyed, f"no path:line key for {OPS_PATH}; this test proves nothing"
    _ship(payload_dir, OPS_PATH, [line + shift for line in keyed], max(keyed) + shift + 1)
    return keyed


@pytest.mark.requirement("INSTALL-11")
def test_a_dynamic_key_on_a_present_site_passes_the_scan(tmp_path: Path) -> None:
    """[if] each keyed _ops.py line holds its site [then] the scan classifies it, [else stop]."""
    keyed = _ship_ops(tmp_path)
    sites, classified = scan_runtime_loaded_libraries(tmp_path)
    assert sorted(site.line for site in sites) == keyed
    for line in keyed:
        assert classified[f"{OPS_PATH}:{line} CDLL(<dynamic>)"] == RUNTIME_LOAD_ALLOWLIST[f"{OPS_PATH}:{line}"]


@pytest.mark.requirement("INSTALL-11")
def test_a_dynamic_key_whose_line_moved_fails_the_scan_as_stale(tmp_path: Path) -> None:
    """[if] a bump moves the keyed site one line [then] the scan names the old key stale, [else stop]."""
    keyed = _ship_ops(tmp_path, shift=1)
    with pytest.raises(PayloadBuildError) as excinfo:
        scan_runtime_loaded_libraries(tmp_path)
    message = str(excinfo.value)
    assert "stale RUNTIME_LOAD_ALLOWLIST entries" in message
    listed = {row.strip() for row in message.splitlines()}
    for line in keyed:
        assert f"{OPS_PATH}:{line}" in listed


@pytest.mark.requirement("INSTALL-11")
def test_an_unlisted_torch_load_discovered_by_the_scan_fails_as_unclassified(
    tmp_path: Path,
) -> None:
    """[if] the scan finds an unlisted torch load [then] it fails as unclassified, [else stop]."""
    _ship_ops(tmp_path)
    probe = tmp_path / "pylib" / "torch" / "_future_probe.py"
    probe.write_text('ctypes.CDLL("unknown_cuda_thing")\n', encoding="utf-8")
    with pytest.raises(PayloadBuildError) as excinfo:
        scan_runtime_loaded_libraries(tmp_path)
    message = str(excinfo.value)
    assert "unclassified runtime library loads" in message
    assert "pylib/torch/_future_probe.py:1 CDLL(unknown_cuda_thing)" in message
    assert "stale RUNTIME_LOAD_ALLOWLIST entries" not in message
