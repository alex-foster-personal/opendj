"""Worktree port reservation and collision regression tests."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


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
    backend = _free_port()
    frontend = _free_port()
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
    backend = _free_port()
    frontend = _free_port()
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
    backend = _free_port()
    frontend = _free_port()
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
    backend = _free_port()
    frontend = _free_port()
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
