"""Unit tests for :mod:`apps.sync.fingerprint` (SYNC-02).

We never invoke the real chromaprint backend here; we monkeypatch the
``acoustid`` shim so these tests run everywhere regardless of whether
``libchromaprint`` is installed.
"""
from __future__ import annotations

import builtins
import sys
import types
import warnings
from pathlib import Path

import pytest

from apps.sync import fingerprint as fp_mod
from apps.sync.fingerprint import FingerprintCache, compare_pair


pytestmark = pytest.mark.requirement("SYNC-02")


@pytest.fixture(autouse=True)
def reset_backend_warning() -> None:
    fp_mod._BACKEND_WARNED = False


@pytest.fixture
def cache(tmp_path: Path) -> FingerprintCache:
    c = FingerprintCache(tmp_path / "fp.sqlite")
    try:
        yield c
    finally:
        c.close()


class _FakeAcoustid(types.ModuleType):
    NoBackendError = type("NoBackendError", (Exception,), {})

    def __init__(self) -> None:
        super().__init__("acoustid")
        self.calls: list[str] = []
        self.compare_calls: list[tuple] = []

    def fingerprint_file(self, path: str):  # noqa: D401
        self.calls.append(path)
        return (180.0, f"FAKE_FP:{Path(path).name}")

    def compare_fingerprints(self, a: tuple, b: tuple) -> float:
        self.compare_calls.append((a, b))
        # simple equality -> 1.0, else 0.5
        return 1.0 if a[1] == b[1] else 0.5


@pytest.fixture
def fake_acoustid(monkeypatch: pytest.MonkeyPatch) -> _FakeAcoustid:
    mod = _FakeAcoustid()
    monkeypatch.setitem(sys.modules, "acoustid", mod)
    return mod


class TestCacheSchema:
    def test_creates_sqlite_schema(self, tmp_path: Path) -> None:
        c = FingerprintCache(tmp_path / "fp.sqlite")
        try:
            cur = c._con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cur.fetchall()}
            assert "fingerprints" in tables
        finally:
            c.close()


class TestGetOrCompute:
    def test_cache_miss_calls_backend(
        self, cache: FingerprintCache, fake_acoustid: _FakeAcoustid, tmp_path: Path
    ) -> None:
        song = tmp_path / "Song.mp3"
        song.touch()
        out = cache.get_or_compute(song)
        assert out == (180.0, f"FAKE_FP:Song.mp3")
        assert fake_acoustid.calls == [str(song)]

    def test_cache_hit_skips_backend(
        self, cache: FingerprintCache, fake_acoustid: _FakeAcoustid, tmp_path: Path
    ) -> None:
        song = tmp_path / "Song.mp3"
        song.touch()
        cache.get_or_compute(song)
        # Clear call log; second call should not hit backend again.
        fake_acoustid.calls.clear()
        out = cache.get_or_compute(song)
        assert out == (180.0, f"FAKE_FP:Song.mp3")
        assert fake_acoustid.calls == []

    def test_no_backend_warns_once_and_caches_empty(
        self,
        cache: FingerprintCache,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Force the `import acoustid` line inside get_or_compute to fail.
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "acoustid", None)
        real_import = builtins.__import__

        def raising(name, *a, **kw):
            if name == "acoustid":
                raise ImportError("no acoustid")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", raising)
        song = tmp_path / "S.mp3"
        song.touch()
        with warnings.catch_warnings(record=True) as rec:
            warnings.simplefilter("always")
            out1 = cache.get_or_compute(song)
            out2 = cache.get_or_compute(tmp_path / "Other.mp3")
        assert out1 == (0.0, "")
        # Second call was a cache miss on a different path; it warns too.
        # The one-shot guard is per-process; the first call marked the flag.
        # So only ONE warning should have been emitted total.
        backend_warnings = [w for w in rec if "backend missing" in str(w.message)]
        assert len(backend_warnings) == 1


class TestCompare:
    def test_compare_returns_similarity(
        self, cache: FingerprintCache, fake_acoustid: _FakeAcoustid, tmp_path: Path
    ) -> None:
        a = tmp_path / "a.mp3"
        b = tmp_path / "b.mp3"
        a.touch()
        b.touch()
        score = cache.compare(a, b)
        assert score == 0.5  # different names -> different fake fingerprints

    def test_compare_none_if_path_missing(
        self, cache: FingerprintCache, fake_acoustid: _FakeAcoustid
    ) -> None:
        assert cache.compare(None, None) is None


class TestComparePairSignal:
    def test_signal_name_and_weight(
        self, cache: FingerprintCache, fake_acoustid: _FakeAcoustid, tmp_path: Path
    ) -> None:
        a = tmp_path / "a.mp3"
        a.touch()
        signal = compare_pair(a, a, cache=cache)
        assert signal.name == "chromaprint"
        assert signal.weight == pytest.approx(0.30)
        assert signal.fired  # identical path -> 1.0 >= 0.90
