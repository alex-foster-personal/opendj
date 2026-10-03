"""Issue #205 acceptance tests for the safe Pioneer USB export workflow.

Requirements: usb-export, CAT-06.

[if] a target is not an unambiguous disposable USB volume [then ⛔️] planning
fails before any export path is created.
[if] target identity, plan bytes, or template bytes drift before apply
[then ⛔️] apply fails before promotion.
[if] a real OneLibrary overlay is applied to an empty disposable target
[then ⛔️] mounted-target readback must match before success is reported.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from apps.sync.usb.pioneer import export_workflow as workflow
from apps.sync.usb.pioneer import writer_rbox
from apps.sync.usb.pioneer.writer_rbox import PlaylistSpec, TrackUpdate
from tests.fixtures.conftest import resolve_required_fixture

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("CAT-06"), pytest.mark.rekordbox_writeback]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_db() -> Path:
    """Resolve ``tests/fixtures/rb-usb-export/PIONEER/rekordbox/exportLibrary.db`` lazily.

    Routes through resolve_required_fixture() (rather than a hard-coded
    repo path) so this CAT-06 acceptance module fails closed on a missing
    fixture host instead of silently breaking, once the in-repo directory
    leaves and only ``rb-usb-export.extern`` remains (PR #718). Deferred
    out of a module-level constant into this helper (called only from the
    fixture-dependent test bodies below) so an
    ``MDT_ALLOW_MISSING_FIXTURES=1`` skip -- or a missing host with no
    opt-out, which fails closed via ``resolve_required_fixture`` -- drops
    only the tests that actually need USB data, not the whole module
    (PR #718 review). test_rbox_dependency_contract_is_pinned_for_ci,
    the platform-refusal tests, and marker validation need no fixture and
    must stay collectible either way.
    """
    return (
        resolve_required_fixture("rb-usb-export") / "PIONEER" / "rekordbox" / "exportLibrary.db"
    )


def _identity(target: Path, *, uuid: str = "USB-205") -> workflow.TargetIdentity:
    return workflow.TargetIdentity(
        root=target.resolve(),
        volume_label=target.name,
        volume_uuid=uuid,
        authorization_id="issue-205-disposable",
    )


def _require_rbox_runtime() -> None:
    """Fail USB integration tests at the dependency contract boundary."""
    if not writer_rbox.RBOX_AVAILABLE:
        pytest.fail(
            "USB export integration requires the pinned runtime dependency "
            "rbox==0.1.7. Install the repository dependency contract before "
            f"running these tests: {writer_rbox.RBOX_IMPORT_ERROR}",
            pytrace=False,
        )


def _promote_exclusively_on_test_filesystem(
    source: Path, destination: Path
) -> None:
    """Exercise real no-replace promotion without claiming host USB support."""
    os.link(source, destination)
    source.unlink()


@pytest.fixture
def platform_neutral_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow, "_rename_exclusive", _promote_exclusively_on_test_filesystem
    )


def test_rbox_dependency_contract_is_pinned_for_ci() -> None:
    """CI and the dev extra install rbox; the core deps and the DMG do not.

    rbox is GPL-3.0-only (issue #5143). Fresh Windows parity environments
    and Linux CI still get the pin through the dev extra and requirements.txt.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    assert "rbox==0.1.7" not in pyproject["project"]["dependencies"]
    optional = pyproject["project"]["optional-dependencies"]
    assert optional["usb-export"] == ["rbox==0.1.7"]
    assert "rbox==0.1.7" in optional["dev"]
    requirements = (REPO_ROOT / "requirements.txt").read_text("utf-8").splitlines()
    assert "rbox==0.1.7" in requirements, (
        "requirements.txt must pin rbox==0.1.7 so the Linux CI job installs "
        "the OneLibrary runtime."
    )


@pytest.fixture
def disposable_target(tmp_path: Path) -> Path:
    target = tmp_path / "DISPOSABLE-205"
    target.mkdir()
    (target / workflow.DISPOSABLE_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "disposable": True,
                "volume_label": target.name,
                "volume_uuid": "USB-205",
                "authorization_id": "issue-205-disposable",
            }
        ),
        encoding="utf-8",
    )
    return target


def test_plan_is_deterministic_and_serializable(
    disposable_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    kwargs = {
        "template_path": _fixture_db(),
        "target_root": disposable_target,
        "playlists": [PlaylistSpec(name="Issue 205", track_ids=(1, 2, 3))],
        "track_updates": [TrackUpdate(id=1, title="Issue 205 title")],
    }

    first = workflow.plan_export(**kwargs)
    second = workflow.plan_export(**kwargs)

    assert first == second
    assert workflow.ExportPlan.from_dict(first.to_dict()) == first
    assert first.plan_id == workflow.compute_plan_id(first.to_dict())
    assert first.scope == "onelibrary_overlay_only"
    assert workflow.OUTPUT_RELATIVE_PATH.as_posix() == (
        "PIONEER/rekordbox/exportLibrary.db"
    )
    assert not (disposable_target / "PIONEER").exists()


def test_overlay_output_path_rejected_by_stick_value_verifier(tmp_path: Path) -> None:
    """Fake applied overlay tree is not a rekordbox export."""
    from apps.sync.usb.pioneer.value_verify import verify_stick_values

    target = tmp_path / "stick"
    output = target / workflow.OUTPUT_RELATIVE_PATH
    output.parent.mkdir(parents=True)
    output.write_bytes(b"overlay-only")
    report = verify_stick_values(target)
    assert report.is_rekordbox_export is False
    assert report.tracks_on_stick == 0


def test_real_inspector_refuses_non_macos_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ordinary-directory"
    target.mkdir()
    monkeypatch.setattr(workflow.sys, "platform", "linux")

    with pytest.raises(workflow.UsbExportError, match="macOS") as exc_info:
        workflow.inspect_macos_target(target)

    assert exc_info.value.code == "platform_unsupported"
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_exclusive_promotion_refuses_non_macos_without_moving_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    source = tmp_path / "source.part"
    destination = tmp_path / "exportLibrary.db"
    source.write_bytes(b"transaction")
    monkeypatch.setattr(workflow.sys, "platform", platform)

    with pytest.raises(workflow.UsbExportError, match="macOS only") as exc_info:
        workflow._rename_exclusive(source, destination)

    assert exc_info.value.code == "platform_unsupported"
    assert source.read_bytes() == b"transaction"
    assert not destination.exists()


@pytest.mark.parametrize(
    ("marker_update", "error_code"),
    [
        ({"disposable": False}, "target_not_disposable"),
        ({"volume_uuid": "DIFFERENT"}, "target_identity_mismatch"),
        ({"volume_label": "DIFFERENT"}, "target_identity_mismatch"),
        ({"authorization_id": ""}, "target_marker_invalid"),
    ],
)
def test_marker_must_explicitly_match_live_identity(
    disposable_target: Path,
    marker_update: dict[str, object],
    error_code: str,
) -> None:
    marker_path = disposable_target / workflow.DISPOSABLE_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.update(marker_update)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.validate_disposable_marker(
            disposable_target,
            volume_label=disposable_target.name,
            volume_uuid="USB-205",
        )

    assert exc_info.value.code == error_code


def test_apply_refuses_identity_drift_before_creating_export(
    disposable_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
        playlists=[PlaylistSpec(name="Issue 205", track_ids=(1,))],
    )
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target), uuid="USB-CHANGED"),
    )

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.apply_export(plan, confirmation=plan.plan_id)

    assert exc_info.value.code == "target_identity_changed"
    assert not (disposable_target / "PIONEER").exists()


def test_apply_refuses_existing_payload_without_modifying_it(
    disposable_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
        playlists=[PlaylistSpec(name="Issue 205", track_ids=(1,))],
    )
    existing = disposable_target / "do-not-overwrite.txt"
    existing.write_bytes(b"valuable")

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.apply_export(plan, confirmation=plan.plan_id)

    assert exc_info.value.code == "target_not_empty"
    assert existing.read_bytes() == b"valuable"
    assert not (disposable_target / "PIONEER").exists()


def test_apply_and_readback_real_onelibrary_round_trip(
    disposable_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_neutral_promotion: None,
) -> None:
    _require_rbox_runtime()
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
        playlists=[PlaylistSpec(name="Issue 205", track_ids=(1, 2, 3))],
        track_updates=[
            TrackUpdate(id=1, title="Issue 205 title", rating=5, bpmx100=12600)
        ],
    )

    receipt = workflow.apply_export(plan, confirmation=plan.plan_id)
    report = workflow.readback_export(plan, receipt)

    output = disposable_target / workflow.OUTPUT_RELATIVE_PATH
    assert output.is_file()
    assert receipt.verified is True
    assert report.verified is True
    assert report.output_sha256 == receipt.output_sha256
    assert report.playlists[0]["name"] == "Issue 205"
    assert report.playlists[0]["track_ids"] == [1, 2, 3]
    assert report.track_updates[0]["title"] == "Issue 205 title"


def test_apply_requires_exact_confirmation(
    disposable_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
    )

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.apply_export(plan, confirmation="wrong-plan")

    assert exc_info.value.code == "plan_confirmation_mismatch"
    assert not (disposable_target / "PIONEER").exists()


def test_plan_rejects_tampered_serialized_payload(
    disposable_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
    ).to_dict()
    plan["target_root"] = str(disposable_target / "other")

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.ExportPlan.from_dict(plan)

    assert exc_info.value.code == "plan_digest_mismatch"


def test_cli_plan_apply_readback_uses_same_serializable_contract(
    disposable_target: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform_neutral_promotion: None,
) -> None:
    _require_rbox_runtime()
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda target: _identity(Path(target)),
    )
    plan_path = tmp_path / "plan.json"
    receipt_path = tmp_path / "receipt.json"

    plan_rc = workflow.main(
        [
            "plan",
            "--template",
            str(_fixture_db()),
            "--target",
            str(disposable_target),
            "--playlist",
            "CLI 205:1,2",
            "--out",
            str(plan_path),
        ]
    )
    plan_stdout = json.loads(capsys.readouterr().out)
    assert plan_rc == 0
    assert json.loads(plan_path.read_text(encoding="utf-8")) == plan_stdout

    apply_rc = workflow.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm",
            plan_stdout["plan_id"],
            "--receipt-out",
            str(receipt_path),
        ]
    )
    receipt_stdout = json.loads(capsys.readouterr().out)
    assert apply_rc == 0
    assert receipt_stdout["verified"] is True
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt_stdout

    readback_rc = workflow.main(
        [
            "readback",
            "--plan",
            str(plan_path),
            "--receipt",
            str(receipt_path),
        ]
    )
    readback_stdout = json.loads(capsys.readouterr().out)
    assert readback_rc == 0
    assert readback_stdout["verified"] is True
    assert readback_stdout["playlists"][0]["track_ids"] == [1, 2]


def test_cli_reports_platform_gap_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "ordinary"
    target.mkdir()
    monkeypatch.setattr(workflow.sys, "platform", "linux")

    rc = workflow.main(
        [
            "plan",
            "--template",
            str(_fixture_db()),
            "--target",
            str(target),
        ]
    )

    stderr = json.loads(capsys.readouterr().err)
    assert rc == 3
    assert stderr["ok"] is False
    assert stderr["error"]["code"] == "platform_unsupported"


def test_promotion_preserves_competing_part_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_output = tmp_path / "local.db"
    local_output.write_bytes(b"transaction")
    target_output = (
        tmp_path / "target" / "PIONEER" / "rekordbox" / "exportLibrary.db"
    )
    real_open = Path.open

    def collide_on_part(path: Path, *args: object, **kwargs: object):
        if args and "x" in str(args[0]) and path.name.endswith(".part"):
            path.write_bytes(b"competing")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", collide_on_part)

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow._promote_new_file(local_output, target_output)

    assert exc_info.value.code == "target_output_exists"
    part_files = list(target_output.parent.glob("*.part"))
    assert len(part_files) == 1
    assert part_files[0].read_bytes() == b"competing"


def test_identity_drift_after_promotion_refuses_rollback_deletion(
    disposable_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_neutral_promotion: None,
) -> None:
    _require_rbox_runtime()
    original = _identity(disposable_target)
    monkeypatch.setattr(workflow, "inspect_macos_target", lambda target: original)
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
        playlists=[PlaylistSpec(name="Identity drift", track_ids=(1,))],
    )
    probe_count = 0

    def drifting_probe(target: str | Path) -> workflow.TargetIdentity:
        nonlocal probe_count
        probe_count += 1
        if probe_count <= 2:
            return original
        return _identity(Path(target), uuid="REPLACEMENT-VOLUME")

    monkeypatch.setattr(workflow, "inspect_macos_target", drifting_probe)

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.apply_export(plan, confirmation=plan.plan_id)

    assert exc_info.value.code == "rollback_refused"
    assert (disposable_target / workflow.OUTPUT_RELATIVE_PATH).is_file()


def test_oserror_during_mounted_readback_rolls_back_exact_output(
    disposable_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_neutral_promotion: None,
) -> None:
    _require_rbox_runtime()
    identity = _identity(disposable_target)
    monkeypatch.setattr(workflow, "inspect_macos_target", lambda target: identity)
    plan = workflow.plan_export(
        template_path=_fixture_db(),
        target_root=disposable_target,
        playlists=[PlaylistSpec(name="Readback error", track_ids=(1,))],
    )

    def fail_readback(
        plan: workflow.ExportPlan,
        receipt: workflow.ApplyReceipt,
    ) -> workflow.ReadbackReport:
        raise OSError("device read failed")

    monkeypatch.setattr(workflow, "readback_export", fail_readback)

    with pytest.raises(workflow.UsbExportError) as exc_info:
        workflow.apply_export(plan, confirmation=plan.plan_id)

    assert exc_info.value.code == "readback_failed"
    assert not (disposable_target / workflow.OUTPUT_RELATIVE_PATH).exists()
