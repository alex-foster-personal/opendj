"""Fail-closed OneLibrary export plan, apply, and readback workflow.

This issue #205 slice intentionally writes only the existing Prototype B
OneLibrary overlay at ``PIONEER/rekordbox/exportLibrary.db``. It does not claim
to generate a complete classic ``export.pdb``/ANLZ/audio export. This workflow
does not produce a gig stick; overlay-only output is rejected by
``python -m apps.sync.usb.verify --pioneer-export``.

Live target discovery is macOS-only. A target is accepted only when ``diskutil``
identifies the exact ``/Volumes/<label>`` mount as external USB media and an
explicit disposable marker matches its label and volume UUID. Apply rechecks
all identity and content-addressed plan evidence before touching the volume.

CLI::

    python -m apps.sync.usb.pioneer.export_workflow plan ...
    python -m apps.sync.usb.pioneer.export_workflow apply ...
    python -m apps.sync.usb.pioneer.export_workflow readback ...
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import errno
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.shared import macos_diskutil
from apps.shared.rekordbox_writeback import require_writeback_enabled

from . import writer_rbox
from .writer_rbox import PlaylistSpec, TrackUpdate

SCHEMA_VERSION = 1
DISPOSABLE_MARKER_NAME = ".mdj-disposable-usb.json"
OUTPUT_RELATIVE_PATH = Path("PIONEER") / "rekordbox" / "exportLibrary.db"
SCOPE = "onelibrary_overlay_only"
_SYSTEM_VOLUME_ENTRIES = frozenset(
    {DISPOSABLE_MARKER_NAME, ".Spotlight-V100", ".Trashes", ".fseventsd"}
)
_MARKER_KEYS = frozenset(
    {
        "schema_version",
        "disposable",
        "volume_label",
        "volume_uuid",
        "authorization_id",
    }
)


class UsbExportError(RuntimeError):
    """Structured, user-actionable workflow failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclasses.dataclass(frozen=True)
class TargetIdentity:
    """Unambiguous live USB volume identity plus explicit authorization."""

    root: Path
    volume_label: str
    volume_uuid: str
    authorization_id: str


@dataclasses.dataclass(frozen=True)
class ExportPlan:
    """Content-addressed, serializable export intent."""

    plan_id: str
    schema_version: int
    scope: str
    template_path: str
    template_sha256: str
    target_root: str
    volume_label: str
    volume_uuid: str
    authorization_id: str
    output_relative_path: str
    playlists: tuple[PlaylistSpec, ...]
    track_updates: tuple[TrackUpdate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "scope": self.scope,
            "template_path": self.template_path,
            "template_sha256": self.template_sha256,
            "target_root": self.target_root,
            "volume_label": self.volume_label,
            "volume_uuid": self.volume_uuid,
            "authorization_id": self.authorization_id,
            "output_relative_path": self.output_relative_path,
            "playlists": [
                {
                    "name": playlist.name,
                    "track_ids": [int(track_id) for track_id in playlist.track_ids],
                    "parent_id": playlist.parent_id,
                }
                for playlist in self.playlists
            ],
            "track_updates": [
                {"id": update.id, **update.to_overlay()}
                for update in self.track_updates
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExportPlan:
        expected_keys = {field.name for field in dataclasses.fields(cls)}
        unknown = set(payload) - expected_keys
        missing = expected_keys - set(payload)
        if unknown or missing:
            raise UsbExportError(
                "plan_schema_invalid",
                f"plan keys mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}",
            )
        try:
            playlists = tuple(
                PlaylistSpec(
                    name=str(item["name"]),
                    track_ids=tuple(int(value) for value in item["track_ids"]),
                    parent_id=(
                        None
                        if item.get("parent_id") is None
                        else int(item["parent_id"])
                    ),
                )
                for item in payload["playlists"]
            )
            track_updates = tuple(
                TrackUpdate(**dict(item)) for item in payload["track_updates"]
            )
            plan = cls(
                plan_id=str(payload["plan_id"]),
                schema_version=int(payload["schema_version"]),
                scope=str(payload["scope"]),
                template_path=str(payload["template_path"]),
                template_sha256=str(payload["template_sha256"]),
                target_root=str(payload["target_root"]),
                volume_label=str(payload["volume_label"]),
                volume_uuid=str(payload["volume_uuid"]),
                authorization_id=str(payload["authorization_id"]),
                output_relative_path=str(payload["output_relative_path"]),
                playlists=playlists,
                track_updates=track_updates,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UsbExportError("plan_schema_invalid", f"invalid plan: {exc}") from exc
        if plan.schema_version != SCHEMA_VERSION or plan.scope != SCOPE:
            raise UsbExportError(
                "plan_schema_invalid",
                f"unsupported plan schema/scope: {plan.schema_version}/{plan.scope}",
            )
        if plan.output_relative_path != OUTPUT_RELATIVE_PATH.as_posix():
            raise UsbExportError(
                "plan_schema_invalid",
                f"unexpected output path: {plan.output_relative_path}",
            )
        if compute_plan_id(plan.to_dict()) != plan.plan_id:
            raise UsbExportError("plan_digest_mismatch", "plan payload was modified")
        return plan


@dataclasses.dataclass(frozen=True)
class ApplyReceipt:
    """Evidence returned only after mounted-target readback succeeds."""

    plan_id: str
    volume_uuid: str
    output_relative_path: str
    output_sha256: str
    playlist_ids: tuple[int, ...]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "volume_uuid": self.volume_uuid,
            "output_relative_path": self.output_relative_path,
            "output_sha256": self.output_sha256,
            "playlist_ids": list(self.playlist_ids),
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ApplyReceipt:
        try:
            return cls(
                plan_id=str(payload["plan_id"]),
                volume_uuid=str(payload["volume_uuid"]),
                output_relative_path=str(payload["output_relative_path"]),
                output_sha256=str(payload["output_sha256"]),
                playlist_ids=tuple(int(value) for value in payload["playlist_ids"]),
                verified=bool(payload["verified"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UsbExportError(
                "receipt_schema_invalid", f"invalid receipt: {exc}"
            ) from exc


@dataclasses.dataclass(frozen=True)
class ReadbackReport:
    """Independent read model for an applied export."""

    plan_id: str
    volume_uuid: str
    output_relative_path: str
    output_sha256: str
    playlists: tuple[dict[str, Any], ...]
    track_updates: tuple[dict[str, Any], ...]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "volume_uuid": self.volume_uuid,
            "output_relative_path": self.output_relative_path,
            "output_sha256": self.output_sha256,
            "playlists": list(self.playlists),
            "track_updates": list(self.track_updates),
            "verified": self.verified,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_plan_id(payload: Mapping[str, Any]) -> str:
    """Return the digest for a plan mapping, ignoring its ``plan_id`` field."""
    canonical = dict(payload)
    canonical.pop("plan_id", None)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_disposable_marker(
    target_root: str | Path,
    *,
    volume_label: str,
    volume_uuid: str,
) -> str:
    """Validate the explicit disposable-volume authorization marker."""
    marker_path = Path(target_root) / DISPOSABLE_MARKER_NAME
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsbExportError(
            "target_marker_missing",
            f"missing disposable marker: {marker_path}",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UsbExportError(
            "target_marker_invalid",
            f"unreadable disposable marker {marker_path}: {exc}",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _MARKER_KEYS:
        raise UsbExportError(
            "target_marker_invalid",
            f"disposable marker must contain exactly {sorted(_MARKER_KEYS)}",
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise UsbExportError(
            "target_marker_invalid",
            f"unsupported disposable marker schema: {payload['schema_version']!r}",
        )
    if payload["disposable"] is not True:
        raise UsbExportError(
            "target_not_disposable",
            "marker does not explicitly declare disposable=true",
        )
    authorization_id = payload["authorization_id"]
    if not isinstance(authorization_id, str) or not authorization_id.strip():
        raise UsbExportError(
            "target_marker_invalid", "authorization_id must be a non-empty string"
        )
    if payload["volume_label"] != volume_label or payload["volume_uuid"] != volume_uuid:
        raise UsbExportError(
            "target_identity_mismatch",
            "disposable marker label/UUID does not match the live volume",
        )
    return authorization_id


def _target_payload_entries(target_root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            child.name
            for child in target_root.iterdir()
            if child.name not in _SYSTEM_VOLUME_ENTRIES
        )
    )


def _require_empty_target(target_root: Path) -> None:
    entries = _target_payload_entries(target_root)
    if entries:
        raise UsbExportError(
            "target_not_empty",
            "refusing target with existing payload entries: " + ", ".join(entries),
        )


def inspect_macos_target(target_root: str | Path) -> TargetIdentity:
    """Prove target identity from live macOS ``diskutil`` data."""
    if sys.platform != "darwin":
        raise UsbExportError(
            "platform_unsupported",
            "live USB export apply/readback requires macOS diskutil verification",
        )
    requested = Path(target_root).expanduser()
    if requested.is_symlink():
        raise UsbExportError("target_ambiguous", "target root must not be a symlink")
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UsbExportError(
            "target_missing", f"target does not exist: {requested}"
        ) from exc
    if not root.is_dir() or root.parent != Path("/Volumes"):
        raise UsbExportError(
            "target_ambiguous",
            f"target must be one direct /Volumes child, got {root}",
        )
    try:
        process = subprocess.run(
            [str(macos_diskutil.DISKUTIL_PATH), "info", "-plist", str(root)],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UsbExportError(
            "target_probe_failed", f"diskutil probe failed: {exc}"
        ) from exc
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        raise UsbExportError(
            "target_probe_failed", f"diskutil rejected {root}: {stderr}"
        )
    try:
        info = plistlib.loads(process.stdout)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise UsbExportError(
            "target_probe_failed", f"invalid diskutil plist: {exc}"
        ) from exc
    mount_point = info.get("MountPoint")
    volume_label = info.get("VolumeName")
    volume_uuid = info.get("VolumeUUID")
    bus_protocol = str(info.get("BusProtocol", "")).upper()
    is_external = info.get("Internal") is False
    is_ejectable = info.get("Ejectable") is True or info.get("RemovableMedia") is True
    is_writable = not info.get("ReadOnlyMedia", False) and not info.get(
        "ReadOnlyVolume", False
    )
    if mount_point != str(root) or not isinstance(volume_label, str):
        raise UsbExportError(
            "target_ambiguous", "diskutil mount point/label does not match target"
        )
    if root.name != volume_label:
        raise UsbExportError(
            "target_ambiguous", "filesystem mount label differs from diskutil label"
        )
    if not isinstance(volume_uuid, str) or not volume_uuid:
        raise UsbExportError(
            "target_ambiguous", "diskutil did not return a volume UUID"
        )
    if bus_protocol != "USB" or not is_external or not is_ejectable:
        raise UsbExportError(
            "target_not_disposable",
            "target must be external, ejectable USB media",
        )
    if not is_writable:
        raise UsbExportError("target_not_writable", "target volume is read-only")
    authorization_id = validate_disposable_marker(
        root, volume_label=volume_label, volume_uuid=volume_uuid
    )
    return TargetIdentity(
        root=root,
        volume_label=volume_label,
        volume_uuid=volume_uuid,
        authorization_id=authorization_id,
    )


def _assert_identity(plan: ExportPlan, identity: TargetIdentity) -> None:
    expected_root = Path(plan.target_root).resolve()
    if (
        identity.root != expected_root
        or identity.volume_label != plan.volume_label
        or identity.volume_uuid != plan.volume_uuid
        or identity.authorization_id != plan.authorization_id
    ):
        raise UsbExportError(
            "target_identity_changed",
            "live target identity no longer matches the reviewed plan",
        )
    marker_authorization = validate_disposable_marker(
        identity.root,
        volume_label=identity.volume_label,
        volume_uuid=identity.volume_uuid,
    )
    if marker_authorization != identity.authorization_id:
        raise UsbExportError(
            "target_identity_changed", "disposable authorization changed"
        )


def plan_export(
    *,
    template_path: str | Path,
    target_root: str | Path,
    playlists: Sequence[PlaylistSpec] = (),
    track_updates: Sequence[TrackUpdate] = (),
) -> ExportPlan:
    """Build a deterministic plan without writing to the target."""
    template = Path(template_path).expanduser().resolve()
    if not template.is_file():
        raise UsbExportError("template_missing", f"template not found: {template}")
    identity = inspect_macos_target(target_root)
    _assert_identity(
        ExportPlan(
            plan_id="",
            schema_version=SCHEMA_VERSION,
            scope=SCOPE,
            template_path=str(template),
            template_sha256="",
            target_root=str(identity.root),
            volume_label=identity.volume_label,
            volume_uuid=identity.volume_uuid,
            authorization_id=identity.authorization_id,
            output_relative_path=OUTPUT_RELATIVE_PATH.as_posix(),
            playlists=tuple(playlists),
            track_updates=tuple(track_updates),
        ),
        identity,
    )
    _require_empty_target(identity.root)
    unsigned = ExportPlan(
        plan_id="",
        schema_version=SCHEMA_VERSION,
        scope=SCOPE,
        template_path=str(template),
        template_sha256=_sha256(template),
        target_root=str(identity.root),
        volume_label=identity.volume_label,
        volume_uuid=identity.volume_uuid,
        authorization_id=identity.authorization_id,
        output_relative_path=OUTPUT_RELATIVE_PATH.as_posix(),
        playlists=tuple(playlists),
        track_updates=tuple(track_updates),
    )
    return dataclasses.replace(unsigned, plan_id=compute_plan_id(unsigned.to_dict()))


def _verify_database(
    path: Path,
    *,
    plan: ExportPlan,
    playlist_ids: Sequence[int],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if not writer_rbox.RBOX_AVAILABLE:
        raise UsbExportError(
            "writer_unavailable",
            f"rbox is unavailable: {writer_rbox.RBOX_IMPORT_ERROR}",
        )
    if len(playlist_ids) != len(plan.playlists):
        raise UsbExportError(
            "readback_mismatch", "playlist receipt count differs from plan"
        )
    try:
        db = writer_rbox.OneLibrary(str(path))
        playlist_rows: list[dict[str, Any]] = []
        for playlist_id, expected in zip(playlist_ids, plan.playlists, strict=True):
            playlist = db.get_playlist_by_id(int(playlist_id))
            if playlist is None or str(playlist["name"]) != expected.name:
                raise UsbExportError(
                    "readback_mismatch",
                    f"playlist {playlist_id} name does not match plan",
                )
            contents = db.get_playlist_contents(int(playlist_id))
            track_ids = [int(content["id"]) for content in contents]
            expected_ids = [int(value) for value in expected.track_ids]
            if track_ids != expected_ids:
                raise UsbExportError(
                    "readback_mismatch",
                    f"playlist {playlist_id} membership does not match plan",
                )
            playlist_rows.append(
                {"id": int(playlist_id), "name": expected.name, "track_ids": track_ids}
            )
        track_rows: list[dict[str, Any]] = []
        for expected in plan.track_updates:
            content = db.get_content_by_id(expected.id)
            if content is None:
                raise UsbExportError(
                    "readback_mismatch", f"track {expected.id} is missing"
                )
            actual: dict[str, Any] = {"id": expected.id}
            for field, expected_value in expected.to_overlay().items():
                actual_value = content[field]
                if actual_value != expected_value:
                    raise UsbExportError(
                        "readback_mismatch",
                        f"track {expected.id} field {field} does not match plan",
                    )
                actual[field] = actual_value
            track_rows.append(actual)
        del db
        return tuple(playlist_rows), tuple(track_rows)
    except UsbExportError:
        raise
    except Exception as exc:
        raise UsbExportError("readback_failed", f"rbox readback failed: {exc}") from exc


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Rename without replacing an existing destination."""
    if sys.platform != "darwin":
        raise UsbExportError(
            "platform_unsupported",
            "exclusive USB promotion is implemented for macOS only",
        )
    renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(source),
        os.fsencode(destination),
        0x00000004,  # RENAME_EXCL from macOS sys/stdio.h.
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))


def _cleanup_empty_export_dirs(output_path: Path, target_root: Path) -> None:
    current = output_path.parent
    while current != target_root:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _promote_new_file(local_output: Path, target_output: Path) -> str:
    if target_output.exists():
        raise UsbExportError(
            "target_output_exists", f"refusing to overwrite {target_output}"
        )
    try:
        target_output.parent.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise UsbExportError(
            "target_changed",
            f"export parent appeared after planning: {target_output.parent}",
        ) from exc
    part_path = target_output.with_name(
        f".{target_output.name}.{hashlib.sha256(str(target_output).encode()).hexdigest()[:12]}.part"
    )
    part_created = False
    try:
        with local_output.open("rb") as source, part_path.open("xb") as destination:
            part_created = True
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        expected_hash = _sha256(local_output)
        if _sha256(part_path) != expected_hash:
            raise UsbExportError(
                "promotion_hash_mismatch", "USB staging copy hash does not match"
            )
        _rename_exclusive(part_path, target_output)
        return expected_hash
    except FileExistsError as exc:
        raise UsbExportError(
            "target_output_exists", f"refusing to overwrite {target_output}"
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise UsbExportError(
                "target_output_exists", f"refusing to overwrite {target_output}"
            ) from exc
        raise UsbExportError(
            "promotion_failed", f"USB promotion failed: {exc}"
        ) from exc
    finally:
        if part_created:
            part_path.unlink(missing_ok=True)


def _rollback_promoted_output(
    plan: ExportPlan,
    target_output: Path,
    expected_hash: str,
) -> str | None:
    """Remove only this transaction's bytes from the still-identical volume."""
    try:
        identity = inspect_macos_target(plan.target_root)
        _assert_identity(plan, identity)
        if not target_output.exists():
            return None
        if not target_output.is_file() or _sha256(target_output) != expected_hash:
            return "promoted path no longer contains this transaction's exact bytes"
        target_output.unlink()
        _cleanup_empty_export_dirs(target_output, identity.root)
    except (OSError, UsbExportError) as exc:
        return str(exc)
    return None


def apply_export(plan: ExportPlan, *, confirmation: str) -> ApplyReceipt:
    """Apply a reviewed plan and return only after mounted readback."""
    require_writeback_enabled("module.sync.usb.pioneer.export_workflow")
    ExportPlan.from_dict(plan.to_dict())
    if confirmation != plan.plan_id:
        raise UsbExportError(
            "plan_confirmation_mismatch", "confirmation must equal the exact plan_id"
        )
    identity = inspect_macos_target(plan.target_root)
    _assert_identity(plan, identity)
    _require_empty_target(identity.root)
    template = Path(plan.template_path)
    if not template.is_file() or _sha256(template) != plan.template_sha256:
        raise UsbExportError(
            "template_changed", "template bytes no longer match the reviewed plan"
        )
    target_output = identity.root / OUTPUT_RELATIVE_PATH
    if target_output.exists():
        raise UsbExportError(
            "target_output_exists", f"refusing to overwrite {target_output}"
        )

    with tempfile.TemporaryDirectory(prefix="mdj-usb-export-") as temp_dir:
        staging_root = Path(temp_dir)
        local_template = staging_root / "template.db"
        local_output = staging_root / "exportLibrary.db"
        shutil.copyfile(template, local_template)
        try:
            result = writer_rbox.write_onelibrary(
                template_path=local_template,
                output_path=local_output,
                playlists=plan.playlists,
                track_updates=plan.track_updates,
                overwrite=False,
            )
        except writer_rbox.OneLibraryWriteError as exc:
            raise UsbExportError("writer_failed", str(exc)) from exc
        _verify_database(
            local_output,
            plan=plan,
            playlist_ids=result.playlist_ids,
        )

        final_identity = inspect_macos_target(plan.target_root)
        _assert_identity(plan, final_identity)
        _require_empty_target(final_identity.root)
        output_hash = _promote_new_file(local_output, target_output)

    receipt = ApplyReceipt(
        plan_id=plan.plan_id,
        volume_uuid=plan.volume_uuid,
        output_relative_path=OUTPUT_RELATIVE_PATH.as_posix(),
        output_sha256=output_hash,
        playlist_ids=result.playlist_ids,
        verified=True,
    )
    try:
        readback_export(plan, receipt)
    except Exception as exc:
        rollback_error = _rollback_promoted_output(plan, target_output, output_hash)
        if rollback_error is not None:
            raise UsbExportError(
                "rollback_refused",
                "post-promotion readback failed and automatic cleanup was refused: "
                f"{rollback_error}",
            ) from exc
        if isinstance(exc, UsbExportError):
            raise
        raise UsbExportError(
            "readback_failed", f"post-promotion readback failed: {exc}"
        ) from exc
    return receipt


def readback_export(plan: ExportPlan, receipt: ApplyReceipt) -> ReadbackReport:
    """Independently verify the mounted output against plan and receipt."""
    ExportPlan.from_dict(plan.to_dict())
    if receipt.plan_id != plan.plan_id or receipt.volume_uuid != plan.volume_uuid:
        raise UsbExportError(
            "receipt_mismatch", "receipt identity does not match the plan"
        )
    if receipt.output_relative_path != OUTPUT_RELATIVE_PATH.as_posix():
        raise UsbExportError("receipt_mismatch", "receipt output path is invalid")
    identity = inspect_macos_target(plan.target_root)
    _assert_identity(plan, identity)
    output = identity.root / OUTPUT_RELATIVE_PATH
    if not output.is_file():
        raise UsbExportError("readback_missing", f"export output is missing: {output}")
    output_hash = _sha256(output)
    if output_hash != receipt.output_sha256:
        raise UsbExportError(
            "readback_hash_mismatch", "mounted output hash differs from apply receipt"
        )
    playlists, track_updates = _verify_database(
        output,
        plan=plan,
        playlist_ids=receipt.playlist_ids,
    )
    return ReadbackReport(
        plan_id=plan.plan_id,
        volume_uuid=identity.volume_uuid,
        output_relative_path=OUTPUT_RELATIVE_PATH.as_posix(),
        output_sha256=output_hash,
        playlists=playlists,
        track_updates=track_updates,
        verified=True,
    )


def _parse_playlist(raw: str) -> PlaylistSpec:
    name, separator, ids = raw.rpartition(":")
    if not separator or not name.strip():
        raise UsbExportError("argument_invalid", f"invalid playlist spec: {raw!r}")
    try:
        track_ids = tuple(
            int(value.strip()) for value in ids.split(",") if value.strip()
        )
    except ValueError as exc:
        raise UsbExportError(
            "argument_invalid", f"invalid playlist ids: {raw!r}"
        ) from exc
    return PlaylistSpec(name=name.strip(), track_ids=track_ids)


def _parse_track(raw: str) -> TrackUpdate:
    track_id, separator, fields = raw.partition(":")
    if not separator:
        raise UsbExportError("argument_invalid", f"invalid track spec: {raw!r}")
    values: dict[str, Any] = {"id": int(track_id)}
    allowed = {"title", "rating", "bpmx100", "dj_comment", "color_id"}
    for field_spec in fields.split(","):
        if not field_spec:
            continue
        field, equals, value = field_spec.partition("=")
        if not equals or field not in allowed:
            raise UsbExportError(
                "argument_invalid", f"invalid track field: {field_spec!r}"
            )
        values[field] = (
            int(value) if field in {"rating", "bpmx100", "color_id"} else value
        )
    return TrackUpdate(**values)


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsbExportError(
            "json_input_invalid", f"unable to read {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise UsbExportError(
            "json_input_invalid", f"JSON root must be an object: {path}"
        )
    return payload


def _write_json_new(path: str | Path, payload: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError as exc:
        raise UsbExportError(
            "json_output_exists", f"refusing to overwrite {output}"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe Pioneer OneLibrary USB export")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Inspect target and emit a reviewed plan")
    plan.add_argument("--template", required=True)
    plan.add_argument("--target", required=True)
    plan.add_argument("--playlist", action="append", default=[])
    plan.add_argument("--track", action="append", default=[])
    plan.add_argument("--out")
    apply = commands.add_parser("apply", help="Apply one exact reviewed plan")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm", required=True)
    apply.add_argument("--receipt-out")
    readback = commands.add_parser("readback", help="Verify an applied export")
    readback.add_argument("--plan", required=True)
    readback.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result: ExportPlan | ApplyReceipt | ReadbackReport = plan_export(
                template_path=args.template,
                target_root=args.target,
                playlists=tuple(_parse_playlist(value) for value in args.playlist),
                track_updates=tuple(_parse_track(value) for value in args.track),
            )
            if args.out:
                _write_json_new(args.out, result.to_dict())
        elif args.command == "apply":
            plan = ExportPlan.from_dict(_load_json(args.plan))
            result = apply_export(plan, confirmation=args.confirm)
            if args.receipt_out:
                _write_json_new(args.receipt_out, result.to_dict())
        elif args.command == "readback":
            plan = ExportPlan.from_dict(_load_json(args.plan))
            receipt = ApplyReceipt.from_dict(_load_json(args.receipt))
            result = readback_export(plan, receipt)
        else:
            raise UsbExportError("argument_invalid", f"unknown command: {args.command}")
    except UsbExportError as exc:
        json.dump({"ok": False, "error": exc.to_dict()}, sys.stderr)
        sys.stderr.write("\n")
        return 3 if exc.code.startswith(("target_", "platform_", "plan_")) else 4
    json.dump(result.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
