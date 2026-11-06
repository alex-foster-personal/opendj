"""[if] pre-push step 3b meets a host without the pinned shfmt [then] it installs it or SKIPs loudly, [else stop].

#8904 made step 3b re-prove the shell format commit whenever a branch touches one of its scripts, and a
host with no shfmt then reported UNKNOWN and failed the push. With `--install-shfmt` the proof installs
the pinned release binary on demand (sha256-checked) and retries; offline it prints a SKIP naming
`just shfmt-install`. The downloader is monkeypatched in every test, and an autouse fixture makes any
real network call fail the test.

Regression lines:
  - if ensure_pinned_shfmt reaches the network in a test then broken
  - if an offline install is a silent pass, or a SKIP that does not name `just shfmt-install`, then broken
  - if an offline install without install_missing is anything but UNKNOWN then broken
  - if a download whose sha256 is not pinned is installed, or SKIPs instead of UNKNOWN, then broken
  - if a verified download is not installed into the install dir and returned as the shfmt to use then broken
  - if ignore-revs --install-shfmt passes offline without a SKIP line and a SKIPPED count on its OK line then broken
  - if ignore-revs without --install-shfmt stops reporting UNKNOWN for a missing shfmt then broken
  - if a connection dropped mid-download (IncompleteRead) crashes instead of a SKIP then broken
  - if an install leaves a partial file behind or replaces the binary non-atomically then broken
  - if a verified on-demand install does not let ignore-revs prove the listed shell commit then broken
"""

from __future__ import annotations

import hashlib
import http.client
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from scripts import format_proof, format_proof_shell
from scripts.format_proof_shell import CFG
from tests.quality.format_proof_repo import commit_files, init_repo

pytestmark = [pytest.mark.requirement("OPS-8960")]

BEFORE = "#!/usr/bin/env bash\nif true; then\n  echo hi\nfi\n"
AFTER = "#!/usr/bin/env bash\nif true; then\n    echo hi\nfi\n"
SUBJECT = f"style(format): shfmt@{CFG.SHFMT_VERSION} scripts"
FAKE_SHFMT = f"#!/bin/sh\necho v{CFG.SHFMT_VERSION}\n".encode()
# The installed name on this host: install() adds .exe on Windows.
SHFMT_NAME = "shfmt.exe" if sys.platform == "win32" else "shfmt"
posix_fake = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake shfmt is a #!/bin/sh script, which Windows cannot execute"
)


def _real_pinned_shfmt() -> str | None:
    binary = shutil.which("shfmt")
    if binary is None:
        return None
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return binary if version == f"v{CFG.SHFMT_VERSION}" else None


REAL_SHFMT = _real_pinned_shfmt()


@pytest.fixture(autouse=True)
def no_shfmt_anywhere(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No shfmt on PATH, an empty install dir, and no network: a real download fails the test."""
    real_which = shutil.which
    monkeypatch.setattr(format_proof_shell.shutil, "which", lambda name, **kw: None if name == "shfmt" else real_which(name, **kw))
    install_dir = tmp_path / "local-bin"
    monkeypatch.setattr(CFG, "INSTALL_DIR", install_dir)

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("a test reached urllib.request.urlopen")

    monkeypatch.setattr(format_proof_shell.urllib.request, "urlopen", no_network)
    return install_dir


def _offline(url: str) -> bytes:
    raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")


def _serving(payload: bytes, monkeypatch: pytest.MonkeyPatch, pin: bool = True) -> list[str]:
    """Serve payload as the release asset, pinning its digest for this host's target unless pin is False."""
    urls: list[str] = []

    def download(url: str) -> bytes:
        urls.append(url)
        return payload

    monkeypatch.setattr(format_proof_shell, "_download", download)
    target = format_proof_shell._release_target()
    digest = hashlib.sha256(payload).hexdigest() if pin else "0" * 64
    monkeypatch.setitem(CFG.SHA256, target, digest)
    return urls


@pytest.fixture
def listed_shell_commit(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "repo")
    commit_files(repo, {"a.sh": BEFORE}, "init")
    head = commit_files(repo, {"a.sh": AFTER}, SUBJECT)
    commit_files(repo, {format_proof.CFG.IGNORE_REVS_FILE: f"# shell part\n{head}\n"}, "chore: list it")
    return repo


def test_offline_install_is_a_loud_skip_naming_the_install_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(format_proof_shell, "_download", _offline)
    verdict = format_proof_shell.ensure_pinned_shfmt(install_missing=True)
    assert isinstance(verdict, format_proof_shell.Verdict)
    assert verdict.skipped and verdict.exit_code == 0
    assert verdict.lines[0].startswith("[format-proof] SKIP shell proof NOT RUN"), verdict.lines
    assert "`just shfmt-install`" in verdict.lines[0]


def test_without_install_missing_a_missing_shfmt_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(format_proof_shell, "_download", _offline)
    verdict = format_proof_shell.ensure_pinned_shfmt(install_missing=False)
    assert isinstance(verdict, format_proof_shell.Verdict)
    assert verdict.exit_code == 2 and not verdict.skipped
    assert "UNKNOWN shfmt is not on PATH" in verdict.lines[0]


def test_a_download_with_an_unpinned_digest_is_unknown_and_not_installed(
    monkeypatch: pytest.MonkeyPatch, no_shfmt_anywhere: Path
) -> None:
    _serving(FAKE_SHFMT, monkeypatch, pin=False)
    verdict = format_proof_shell.ensure_pinned_shfmt(install_missing=True)
    assert isinstance(verdict, format_proof_shell.Verdict)
    assert verdict.exit_code == 2 and not verdict.skipped
    assert "failed its sha256 check" in verdict.lines[0]
    assert not (no_shfmt_anywhere / SHFMT_NAME).exists()


@posix_fake
def test_a_verified_download_is_installed_and_used(monkeypatch: pytest.MonkeyPatch, no_shfmt_anywhere: Path) -> None:
    urls = _serving(FAKE_SHFMT, monkeypatch)
    binary = format_proof_shell.ensure_pinned_shfmt(install_missing=True)
    assert binary == str(no_shfmt_anywhere / SHFMT_NAME)
    assert Path(binary).read_bytes() == FAKE_SHFMT
    assert urls == [CFG.RELEASE_URL.format(version=CFG.SHFMT_VERSION, target=format_proof_shell._release_target())]
    assert format_proof_shell.ensure_pinned_shfmt(install_missing=True) == binary, "a second call must not download"
    assert len(urls) == 1


def test_an_unsupported_platform_is_a_loud_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(format_proof_shell.platform, "machine", lambda: "sparc64")
    verdict = format_proof_shell.ensure_pinned_shfmt(install_missing=True)
    assert isinstance(verdict, format_proof_shell.Verdict) and verdict.skipped
    assert "no pinned shfmt digest" in verdict.lines[0] and "`just shfmt-install`" in verdict.lines[0]


def test_ignore_revs_offline_skips_the_shell_proof_loudly(
    monkeypatch: pytest.MonkeyPatch, listed_shell_commit: Path
) -> None:
    monkeypatch.setattr(format_proof_shell, "_download", _offline)
    path = listed_shell_commit / format_proof.CFG.IGNORE_REVS_FILE
    result = format_proof.check_ignore_revs(listed_shell_commit, path, install_shfmt=True)
    assert result.exit_code == 0, result.lines
    assert any(line.startswith("[ignore-revs] SKIP ") for line in result.lines), result.lines
    assert any("`just shfmt-install`" in line for line in result.lines), result.lines
    assert "SHELL PROOF SKIPPED for 1" in result.lines[-1], result.lines


def test_ignore_revs_without_install_flag_stays_unknown(
    monkeypatch: pytest.MonkeyPatch, listed_shell_commit: Path
) -> None:
    monkeypatch.setattr(format_proof_shell, "_download", _offline)
    path = listed_shell_commit / format_proof.CFG.IGNORE_REVS_FILE
    result = format_proof.check_ignore_revs(listed_shell_commit, path)
    assert result.exit_code == 2, result.lines
    assert any("UNKNOWN shfmt is not on PATH" in line for line in result.lines), result.lines


@pytest.mark.skipif(REAL_SHFMT is None, reason=f"needs a real shfmt v{CFG.SHFMT_VERSION} to serve as the download")
def test_ignore_revs_installs_on_demand_and_proves(monkeypatch: pytest.MonkeyPatch, listed_shell_commit: Path) -> None:
    assert REAL_SHFMT is not None
    _serving(Path(REAL_SHFMT).read_bytes(), monkeypatch)
    path = listed_shell_commit / format_proof.CFG.IGNORE_REVS_FILE
    result = format_proof.check_ignore_revs(listed_shell_commit, path, install_shfmt=True)
    assert result.exit_code == 0, result.lines
    assert not any("SKIP" in line for line in result.lines), result.lines


def test_a_connection_dropped_mid_download_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    def truncated(url: str) -> bytes:
        raise http.client.IncompleteRead(b"partial", 3_000_000)

    monkeypatch.setattr(format_proof_shell, "_download", truncated)
    verdict = format_proof_shell.ensure_pinned_shfmt(install_missing=True)
    assert isinstance(verdict, format_proof_shell.Verdict) and verdict.skipped, verdict


def test_install_replaces_the_binary_atomically_and_leaves_no_partial_file(
    monkeypatch: pytest.MonkeyPatch, no_shfmt_anywhere: Path
) -> None:
    no_shfmt_anywhere.mkdir(parents=True)
    (no_shfmt_anywhere / SHFMT_NAME).write_bytes(b"old")
    _serving(FAKE_SHFMT, monkeypatch)
    assert format_proof_shell.install(no_shfmt_anywhere) == 0
    assert sorted(path.name for path in no_shfmt_anywhere.iterdir()) == [SHFMT_NAME]
    assert (no_shfmt_anywhere / SHFMT_NAME).read_bytes() == FAKE_SHFMT
