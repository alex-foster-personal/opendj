"""Local and remote stem stores must remain separate by construction."""

from pathlib import Path

import pytest

from apps.webui.library_assets import ensure_stem_storage, stem_storage


def test_local_mode_keeps_the_existing_state_roots(tmp_path: Path) -> None:
    state = tmp_path / "state"

    storage = stem_storage(
        environ={"MDT_LIBRARY_MODE": "local"},
        state_dir=state,
    )

    assert storage.remote is False
    assert storage.write_root == state / "stems"
    assert storage.roots == (
        state / "stems",
        state / "stems-roformer-spike",
    )


def test_remote_mode_uses_only_crate_derived_roots(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    crate.mkdir()
    local_state = tmp_path / "local-state"

    storage = stem_storage(
        environ={
            "MDT_LIBRARY_MODE": "remote",
            "MDT_CRATE_ROOT": str(crate),
        },
        state_dir=local_state,
    )
    ensure_stem_storage(storage)

    assert storage.remote is True
    assert storage.write_root == crate / "derived" / "stems" / "demucs4"
    assert storage.roots == (
        crate / "derived" / "stems" / "demucs4",
        crate / "derived" / "stems" / "roformer2",
    )
    assert all(root.is_dir() for root in storage.roots)
    assert not local_state.exists()

pytestmark = pytest.mark.rb_parity
