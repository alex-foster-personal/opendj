"""Tests for :class:`apps.shared.platform_paths.AssetResolver`.

Split out of ``test_platform_paths.py`` (pin ad59ac's per-row perf fix, not a
trim of that file's own content -- see ``ops/quality/README.md``'s 600-line
review threshold for why this lives in its own module rather than shrinking
any test or comment).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared import platform_paths as pp

# ----- AssetResolver: per-call containment memo (pin ad59ac) --------------
#
# A prior fix cached the containment verdict for 30s ACROSS REQUESTS and was
# reverted on review: a directory under SHARE_ROOT can become a symlink to
# outside it between two requests, and a cached "safe" verdict would then be
# served without rerunning the check. AssetResolver carries no time
# dimension and no module-level state -- a caller constructs one, threads it
# through the calls making up ONE bulk-hydration pass, and lets it go out of
# scope when that call returns. These tests pin both properties: real
# dedup within one resolver, and no leakage across two separate ones.


def test_asset_resolver_dedupes_repeat_lookup_within_one_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the same candidate is resolved twice through one AssetResolver
    [then ⛔️] the real filesystem walk (Path.resolve) runs once."""
    target = tmp_path / "share" / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")
    expected = target.resolve()  # captured BEFORE patching, so uncounted

    real_resolve = Path.resolve
    calls = {"n": 0}

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        calls["n"] += 1
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT", resolved=target, mapped=True, reason="native"
    )
    resolver = pp.AssetResolver()

    first = resolver.resolve_asset_sibling(mapped, target)
    second = resolver.resolve_asset_sibling(mapped, target)

    assert first.resolved == second.resolved == expected
    assert calls["n"] == 1, (
        f"expected exactly one real Path.resolve() call across two identical "
        f"lookups on one AssetResolver, got {calls['n']}"
    )


def test_asset_resolver_is_keyed_per_candidate(tmp_path: Path) -> None:
    """[if] two different candidates go through one AssetResolver
    [then ⛔️] one path's resolution leaks into the other's result."""
    first_path = tmp_path / "share" / "PIONEER" / "USB" / "a.DAT"
    second_path = tmp_path / "share" / "PIONEER" / "USB" / "b.DAT"
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"a")
    second_path.write_bytes(b"b")
    mapped = pp.MappedPath(
        original="/PIONEER/USB/x.DAT", resolved=None, mapped=True, reason="native"
    )
    resolver = pp.AssetResolver()

    first = resolver.resolve_asset_sibling(mapped, first_path)
    second = resolver.resolve_asset_sibling(mapped, second_path)

    assert first.resolved == first_path.resolve()
    assert second.resolved == second_path.resolve()
    assert first.resolved != second.resolved


def test_asset_resolver_does_not_survive_across_two_instances(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a symlink swap happens BETWEEN two separate AssetResolver
    instances (i.e. two separate build_track_rows calls) [then ⛔️] the
    second must not reuse the first's verdict -- this is the exact property
    the reverted 30s TTL cache violated."""
    share_root = tmp_path / "share"
    target = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ANLZ0000.DAT").write_bytes(b"outside")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT", resolved=target, mapped=True, reason="share"
    )

    first_resolver = pp.AssetResolver()
    first = first_resolver.resolve_asset_sibling(mapped, target)

    replaced_directory = target.parent
    replaced_directory.rename(tmp_path / "USB-before-replacement")
    replaced_directory.symlink_to(outside, target_is_directory=True)

    second_resolver = pp.AssetResolver()
    second = second_resolver.resolve_asset_sibling(mapped, target)

    assert first.resolved == target
    assert second.resolved is None
    assert second.reason == "unsafe:share-symlink"


def test_resolve_asset_path_without_a_resolver_is_uncached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] no AssetResolver is passed [then ⛔️] every lookup re-walks the
    filesystem -- the default, unchanged for every call site but the
    listing hydration path."""
    target = tmp_path / "share" / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")

    real_resolve = Path.resolve
    calls = {"n": 0}

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        calls["n"] += 1
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT", resolved=target, mapped=True, reason="native"
    )

    pp.resolve_asset_sibling(mapped, target)
    pp.resolve_asset_sibling(mapped, target)

    assert calls["n"] == 2, (
        f"expected a real Path.resolve() call on every lookup with no "
        f"resolver, got {calls['n']}"
    )
