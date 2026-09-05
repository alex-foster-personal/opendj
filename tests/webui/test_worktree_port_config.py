"""Worktree port reservation and collision regression tests."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from apps.webui.port_config import (
    PROJECT_ROOT,
    PortConfigError,
    WebuiPorts,
    assert_source_tree_matches_worktree,
    check_reservation,
    claim_ports,
    main,
    resolve_ports,
)


def _write_env(root: Path, backend: int, frontend: int) -> None:
    (root / ".env").write_text(
        "KEEP_THIS=value\n"
        f"MUSIC_DJ_BACKEND_PORT={backend}\n"
        f"MUSIC_DJ_FRONTEND_PORT={frontend}\n"
        f"MUSIC_DJ_API_PROXY_TARGET=http://127.0.0.1:{backend}\n",
        encoding="utf-8",
    )


def _free_ports(count: int) -> list[int]:
    """Allocate `count` DISTINCT free ports.

    Every probe is held open until all of them are bound. Binding one socket,
    reading its port and CLOSING it before binding the next lets the kernel
    hand the same ephemeral port straight back, because a port that was only
    bound (never connected) returns to the pool immediately with no TIME_WAIT.

    That is not theoretical. It broke trunk on 2026-09-05 at 7a0640b88: this
    file called the old one-at-a-time helper twice in a row and the pytest fast
    lane failed with `WebuiPorts(backend=45747, frontend=45747)` -- both calls
    returned 45747, so `claim_ports` correctly refused a config whose backend
    and frontend were the same port.

    It reproduces on Linux and not on macOS, which is why a local check is not
    evidence here: the two kernels allocate ephemeral ports differently. Holding
    the sockets open removes the race on both, because `bind` cannot assign a
    port another live socket already holds.
    """
    probes: list[socket.socket] = []
    try:
        for _ in range(count):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            probes.append(probe)
        return [int(probe.getsockname()[1]) for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def _free_port() -> int:
    return _free_ports(1)[0]


def test_free_ports_holds_every_probe_open_until_all_are_bound() -> None:
    """If ports are allocated in a batch then no probe closes before the last bind.

    Guards the trunk break at 7a0640b88 (2026-09-05): the helper bound one
    probe, read its port and CLOSED it before binding the next, so the kernel
    could hand the same ephemeral port straight back. It did -- the pytest fast
    lane failed with `WebuiPorts(backend=45747, frontend=45747)`.

    This asserts the ORDERING invariant rather than distinctness of the result.
    Distinctness is the symptom and it is a coin flip: a batch of 64 allocated
    with the old racy helper collided in 0 of 40 trials on macOS, so a test that
    checks only distinctness cannot fail on a developer machine and is not a
    guard at all. The ordering is the mechanism, it is what the fix changed, and
    it is identical on every platform.
    """
    events: list[str] = []
    real_socket = socket.socket

    class _Tracking(real_socket):  # type: ignore[misc, valid-type]
        def bind(self, *args: object, **kwargs: object) -> None:
            super().bind(*args, **kwargs)  # type: ignore[misc]
            events.append("bind")

        def close(self) -> None:
            events.append("close")
            super().close()  # type: ignore[misc]

    with mock.patch.object(socket, "socket", _Tracking):
        ports = _free_ports(4)

    assert len(ports) == 4
    assert len(set(ports)) == 4, f"duplicate ports handed out: {sorted(ports)}"
    assert events.count("bind") == 4, f"expected 4 binds, saw {events}"
    first_close = events.index("close")
    last_bind = len(events) - 1 - events[::-1].index("bind")
    assert first_close > last_bind, (
        "a probe was released before the batch finished binding, which is exactly "
        f"the race that broke trunk: {events}"
    )


def test_claim_refuses_a_config_whose_ports_are_equal(tmp_path: Path) -> None:
    """If backend and frontend are the same port then claiming must fail loudly.

    This is the guard that caught the racy allocator rather than letting two
    services be pointed at one port. Keep it: without it the race above would
    have surfaced as a confusing runtime bind failure instead of a clear error.
    """
    _write_env(tmp_path, 45747, 45747)

    with pytest.raises(PortConfigError, match="must be different"):
        resolve_ports(environ={}, dotenv_path=tmp_path / ".env")


def test_root_env_is_the_only_primitive_port_surface(tmp_path: Path) -> None:
    """If two ports are configured then the proxy must be derived, not configured."""
    _write_env(tmp_path, 18697, 19411)

    ports = resolve_ports(environ={}, dotenv_path=tmp_path / ".env")

    assert ports == WebuiPorts(backend=18697, frontend=19411)
    assert ports.api_proxy_target == "http://127.0.0.1:18697"


def test_claim_preserves_unrelated_env_and_removes_redundant_proxy(
    tmp_path: Path,
) -> None:
    """If ports are claimed then secrets and unrelated config must remain untouched."""
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    backend, frontend = _free_ports(2)
    _write_env(repo_root, backend, frontend)

    claimed = claim_ports(
        repo_root=repo_root,
        common_dir=common_dir,
        environ={},
    )

    assert claimed == WebuiPorts(backend=backend, frontend=frontend)
    env_text = (repo_root / ".env").read_text(encoding="utf-8")
    assert "KEEP_THIS=value" in env_text
    assert "MUSIC_DJ_API_PROXY_TARGET" not in env_text
    registry = json.loads(
        (common_dir / "music-dj-tools" / "worktree-ports.json").read_text(encoding="utf-8")
    )
    assert registry["reservations"][str(repo_root.resolve())] == {
        "backend": backend,
        "frontend": frontend,
    }


def test_blank_sample_ports_are_allocated_from_the_central_pool(tmp_path: Path) -> None:
    """If sample ports are blank then the allocator, not a copied default, owns them."""
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    (repo_root / ".env").write_text(
        "MUSIC_DJ_BACKEND_PORT=\nMUSIC_DJ_FRONTEND_PORT=\n",
        encoding="utf-8",
    )

    claimed = claim_ports(
        repo_root=repo_root,
        common_dir=common_dir,
        environ={},
    )

    assert resolve_ports(environ={}, dotenv_path=repo_root / ".env") == claimed


def test_copied_env_is_reallocated_when_another_worktree_owns_the_pair(
    tmp_path: Path,
) -> None:
    """If .env is copied into another worktree then duplicate ports are broken."""
    common_dir = tmp_path / "common"
    common_dir.mkdir()
    first_root = tmp_path / "repo-a"
    second_root = tmp_path / "repo-b"
    first_root.mkdir()
    second_root.mkdir()
    backend, frontend = _free_ports(2)
    _write_env(first_root, backend, frontend)
    _write_env(second_root, backend, frontend)

    first = claim_ports(repo_root=first_root, common_dir=common_dir, environ={})
    second = claim_ports(repo_root=second_root, common_dir=common_dir, environ={})

    assert first == WebuiPorts(backend=backend, frontend=frontend)
    assert second != first
    assert resolve_ports(environ={}, dotenv_path=second_root / ".env") == second


def test_frontend_check_rejects_an_unidentified_backend(tmp_path: Path) -> None:
    """If another process owns the backend port then proxy startup is broken."""
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    frontend = _free_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as backend_socket:
        backend_socket.bind(("127.0.0.1", 0))
        backend_socket.listen()
        backend = int(backend_socket.getsockname()[1])
        _write_env(repo_root, backend, frontend)
        registry_dir = common_dir / "music-dj-tools"
        registry_dir.mkdir()
        (registry_dir / "worktree-ports.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "reservations": {
                        str(repo_root.resolve()): {
                            "backend": backend,
                            "frontend": frontend,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(PortConfigError, match="does not match this worktree"):
            check_reservation(
                "frontend",
                repo_root=repo_root,
                common_dir=common_dir,
                environ={},
            )
        with pytest.raises(PortConfigError, match="backend port .* already in use"):
            check_reservation(
                "backend",
                repo_root=repo_root,
                common_dir=common_dir,
                environ={},
            )


def test_unclaimed_or_foreign_pair_fails_before_binding(tmp_path: Path) -> None:
    """If registry ownership disagrees with .env then service startup is broken."""
    repo_root = tmp_path / "repo-a"
    other_root = tmp_path / "repo-b"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    other_root.mkdir()
    common_dir.mkdir()
    backend, frontend = _free_ports(2)
    _write_env(repo_root, backend, frontend)
    registry_dir = common_dir / "music-dj-tools"
    registry_dir.mkdir()
    (registry_dir / "worktree-ports.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "reservations": {
                    str(other_root.resolve()): {
                        "backend": backend,
                        "frontend": frontend,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PortConfigError, match="not reserved by this worktree"):
        check_reservation(
            "all",
            repo_root=repo_root,
            common_dir=common_dir,
            environ={},
        )


def test_repeat_claim_leaves_dotenv_untouched(tmp_path: Path) -> None:
    """If a claim changes nothing then .env must not be rewritten.

    vite watches the root .env and re-runs claim on every config load, so an
    unconditional rewrite feeds vite its own change event and wedges the dev
    server in a restart loop (hit live Sun 17 Aug 2026: boot 200, then 504
    forever with "frontend port ... already in use" against itself).
    """
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    backend, frontend = _free_ports(2)
    _write_env(repo_root, backend, frontend)

    claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    dotenv = repo_root / ".env"
    first_text = dotenv.read_text(encoding="utf-8")
    first_stat = dotenv.stat()

    claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

    assert dotenv.read_text(encoding="utf-8") == first_text
    assert dotenv.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert dotenv.stat().st_ino == first_stat.st_ino


def _init_git_worktree(root: Path) -> Path:
    """A real Git worktree, so the guard resolves it the way Git does."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root.resolve()


def test_claim_refuses_a_foreign_worktree_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If apps/ came from another worktree then claim must refuse, not rewrite .env."""
    other = _init_git_worktree(tmp_path / "other-worktree")
    _write_env(other, 18777, 19477)
    before = (other / ".env").read_text(encoding="utf-8")
    monkeypatch.chdir(other)

    exit_code = main(["claim"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert str(PROJECT_ROOT.resolve()) in captured.err
    assert str(other) in captured.err
    assert (other / ".env").read_text(encoding="utf-8") == before
    assert not (other / ".git" / "music-dj-tools").exists()


def test_guard_accepts_the_worktree_it_was_loaded_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If apps/ is this worktree's own tree then the guard must stay out of the way."""
    monkeypatch.chdir(PROJECT_ROOT)

    assert assert_source_tree_matches_worktree() == PROJECT_ROOT.resolve()


def test_guard_is_inapplicable_outside_any_git_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the working directory is not in a worktree then no port contract applies."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert assert_source_tree_matches_worktree() is None

pytestmark = pytest.mark.rb_parity
