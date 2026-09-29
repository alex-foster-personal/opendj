"""Worktree port reservation and collision regression tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

import pytest

from apps.webui import port_config
from apps.webui.port_config import (
    BACKEND_ENV,
    BACKEND_POOL_START,
    FRONTEND_ENV,
    FRONTEND_POOL_START,
    POOL_SIZE,
    PORT_LANE_ENV,
    PORT_LANE_STRIDE,
    PROJECT_ROOT,
    RESERVED_FIXED_PORTS,
    PortConfigError,
    WebuiPorts,
    WorktreeUnavailableError,
    _allocate_pair,
    _lane_pool_starts,
    assert_source_tree_matches_worktree,
    check_reservation,
    claim_ports,
    describe_port_owner,
    main,
    release_ports,
    resolve_ports,
    show_ports,
)


def _write_env(root: Path, backend: int, frontend: int) -> None:
    (root / ".env").write_text(
        "KEEP_THIS=value\n"
        f"MUSIC_DJ_BACKEND_PORT={backend}\n"
        f"MUSIC_DJ_FRONTEND_PORT={frontend}\n"
        f"MUSIC_DJ_API_PROXY_TARGET=http://127.0.0.1:{backend}\n",
        encoding="utf-8",
    )


def _free_ports(count: int, exclude: Iterable[int] = ()) -> list[int]:
    """Allocate `count` DISTINCT free ports, none of them in `exclude`.

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

    `exclude` covers the OTHER half of that race, which the batch alone cannot:
    a caller that already holds a port from somewhere else -- a live server
    socket it bound itself -- needs its next port to differ from that one too.
    Trunk broke this way a second time on 2026-09-05 at 9be4e5daa, in
    `test_frontend_check_rejects_an_unidentified_backend`: it allocated the
    frontend from a probe it then closed, and the backend's `bind(0)` was handed
    that exact port straight back, so the assertion for "backend port already in
    use" met "backend and frontend ports must be different" instead. A rejected
    probe is NOT closed before the batch finishes, so the kernel cannot offer
    the same excluded port twice and the loop terminates.
    """
    forbidden = set(exclude)
    probes: list[socket.socket] = []
    chosen: list[int] = []
    try:
        while len(chosen) < count:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            probes.append(probe)
            port = int(probe.getsockname()[1])
            if port not in forbidden:
                chosen.append(port)
        return chosen
    finally:
        for probe in probes:
            probe.close()


def _free_port(exclude: Iterable[int] = ()) -> int:
    return _free_ports(1, exclude=exclude)[0]


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


def test_free_ports_rejects_a_probe_that_lands_on_an_excluded_port() -> None:
    """If the kernel offers a port the caller already holds then the batch binds
    another probe and keeps the rejected one open.

    Guards the second trunk break of 2026-09-05, at 9be4e5daa:
    `test_frontend_check_rejects_an_unidentified_backend` allocated the frontend
    from a probe it CLOSED, then bound the backend with `bind(0)`, and the
    kernel handed that just-released port straight back. Backend equalled
    frontend, so `resolve_ports` raised "backend and frontend ports must be
    different" in place of the error the test was asserting.

    The real kernel cannot be made to collide on demand -- that is a coin flip
    that did not come up once in local trials -- so the collision is EMULATED:
    the first probe reports the excluded port. What is asserted is the
    MECHANISM, not distinctness of the result: a second bind happens, the
    rejected probe is still open when it does (otherwise the kernel could offer
    the same port again), and the excluded port is not returned. A helper with
    no exclusion satisfies a distinctness check almost every run, so distinctness
    alone would not be a guard.
    """
    held = 61234
    events: list[str] = []
    reported: list[int] = []
    real_socket = socket.socket

    class _Racy(real_socket):  # type: ignore[misc, valid-type]
        def bind(self, *args: object, **kwargs: object) -> None:
            super().bind(*args, **kwargs)  # type: ignore[misc]
            events.append("bind")

        def getsockname(self) -> tuple[str, int]:
            host, port = super().getsockname()  # type: ignore[misc]
            if not reported:
                reported.append(held)
                return (host, held)
            return (host, port)

        def close(self) -> None:
            events.append("close")
            super().close()  # type: ignore[misc]

    with mock.patch.object(socket, "socket", _Racy):
        ports = _free_ports(1, exclude=(held,))

    assert reported == [held], "the emulated collision never fired"
    assert ports != [held], f"an excluded port was handed out: {ports}"
    assert events.count("bind") == 2, (
        f"the rejected probe was not replaced by a second bind: {events}"
    )
    first_close = events.index("close")
    last_bind = len(events) - 1 - events[::-1].index("bind")
    assert first_close > last_bind, (
        "the rejected probe was released before the batch finished binding, so "
        f"the kernel could offer that same port again: {events}"
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as backend_socket:
        backend_socket.bind(("127.0.0.1", 0))
        backend_socket.listen()
        backend = int(backend_socket.getsockname()[1])
        # The backend socket is bound and still open, and the frontend port is
        # excluded from it explicitly, so the pair cannot collide by
        # construction. Allocating the frontend FIRST and closing its probe let
        # the kernel hand that port back to `bind(0)` below, which broke trunk
        # at 9be4e5daa (2026-09-05) with "backend and frontend ports must be
        # different" in place of the error this test is about.
        frontend = _free_port(exclude=(backend,))
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
        with pytest.raises(PortConfigError, match=r"backend port .* already in use"):
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


def test_claim_replaces_its_stale_reserved_pair_when_a_listener_owns_it(
    tmp_path: Path,
) -> None:
    """If a previous job left an engine listening then its registry entry is stale.

    The registry is only a coordination hint. A successful claim must still
    bind-test both sockets before returning, or Playwright accepts the claimed
    pair and fails later when its webServer sees the leaked engine.

    This must not assert a fixed slot distance (``replacement.backend ==
    first.backend + 1``): on a self-hosted runner shared by up to fifteen
    concurrent jobs, another job can free slot 0 between this test's two
    claims, so the second claim can land back on the pair the first claim
    skipped. That flaked two PRs within five minutes (runs 34344314949 and
    34344726991, shard 5) with ``assert 8680 == (8681 + 1)``, which is a test
    defect, not a product defect: the ``+ 1`` assertion silently encoded
    "nothing else on the host changes between two claims", which is false on
    a shared host. The invariant that survives a shared host, and the one
    the docstring above actually states, is: the replacement differs from
    the stale pair and stays inside this lane's pool window. Nothing is
    re-probed after the claim: ``_allocate_pair`` closes its bind probes
    before it returns, so a post-claim availability check would recreate
    the same host-timing assumption under a different name.
    """
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    first = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stale_engine:
        stale_engine.bind(("127.0.0.1", first.backend))
        stale_engine.listen()

        replacement = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

        assert replacement != first
        assert replacement.backend != first.backend
        assert replacement.frontend != first.frontend
        backend_start, frontend_start = _lane_pool_starts(0)
        assert backend_start <= replacement.backend < backend_start + POOL_SIZE
        assert frontend_start <= replacement.frontend < frontend_start + POOL_SIZE


def test_claim_keeps_its_pair_while_its_matching_engine_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vite config reload must not move the engine's already-running pair."""
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    first = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    monkeypatch.setattr(
        "apps.webui.port_config._backend_listener_matches_pair",
        lambda ports: ports == first,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as engine:
        engine.bind(("127.0.0.1", first.backend))
        engine.listen()
        assert claim_ports(repo_root=repo_root, common_dir=common_dir, environ={}) == first


@pytest.mark.requirement("INFRA-10")
def test_claim_keeps_the_configured_pair_when_its_own_server_already_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] no registry entry, own server holds the pair [then] it is kept, [else stop].

    A vite config reload with no prior registry entry must not evict its own pair.

    ``configured`` (the pair in ``.env``, or an explicit ``requested`` value) only
    got the ``_backend_listener_matches_pair`` exemption when it also happened to
    equal the REMEMBERED registry pair (``current``). When the registry has no
    entry for this worktree yet, ``current`` is ``None`` and the claim fell
    through to the ``elif configured`` branch, which bind-tested the pair with
    no listener exemption at all. Since vite itself already holds the frontend
    port, the bind test failed and the claim silently moved the worktree to a
    fresh pair out from under its own running engine (observed on the Air's
    preview worktree, ports 8728/9448 -> 8700/9420, Fri 11 Sep 2026).
    """
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    configured = WebuiPorts(backend=BACKEND_POOL_START, frontend=FRONTEND_POOL_START)
    monkeypatch.setattr(
        "apps.webui.port_config._backend_listener_matches_pair",
        lambda ports: ports == configured,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as vite:
        vite.bind(("127.0.0.1", configured.frontend))
        vite.listen()
        claimed = claim_ports(
            repo_root=repo_root,
            common_dir=common_dir,
            environ={},
            requested=configured,
        )

    assert claimed == configured


@pytest.mark.requirement("INFRA-10")
def test_claim_moves_off_the_configured_pair_when_a_foreign_process_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] no registry entry, a foreign process holds it [then] a new pair is used, [else stop].

    The listener exemption must never cover a genuinely foreign process.

    Same starting state as the sibling test above (no registry entry for this
    worktree), but the process on the configured frontend port is NOT this
    worktree's own backend: ``_backend_listener_matches_pair`` says so. The
    claim must move the worktree to a different pair rather than trusting a
    bind failure alone, exactly as it already does for the remembered-pair
    path.
    """
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    configured = WebuiPorts(backend=BACKEND_POOL_START, frontend=FRONTEND_POOL_START)
    monkeypatch.setattr(
        "apps.webui.port_config._backend_listener_matches_pair",
        lambda ports: False,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as foreign:
        foreign.bind(("127.0.0.1", configured.frontend))
        foreign.listen()
        claimed = claim_ports(
            repo_root=repo_root,
            common_dir=common_dir,
            environ={},
            requested=configured,
        )

    assert claimed != configured
    assert claimed.frontend != configured.frontend


def test_two_runner_clones_with_distinct_lanes_never_select_the_same_pair(
    tmp_path: Path,
) -> None:
    """If two independent CI runner clones claim distinct lanes then they must not
    be able to select the same or an overlapping pair.

    Each self-hosted runner is a separate clone with its own common dir and its
    own worktree-ports.json registry (issue #1301, Sat 5 Sep 2026): the lock
    that serializes allocation lives per-registry, so two runners' registries
    never contend, and both start allocating from the same pool base. Without a
    per-runner lane, two runners on the same host land on the identical pair
    the moment both registries are empty, which is exactly what put trunk red
    while every Playwright spec passed.
    """
    runner_a_repo = tmp_path / "runner-a" / "_work" / "music-dj-tools"
    runner_a_common = tmp_path / "runner-a" / "_common"
    runner_b_repo = tmp_path / "runner-b" / "_work" / "music-dj-tools"
    runner_b_common = tmp_path / "runner-b" / "_common"
    for path in (runner_a_repo, runner_a_common, runner_b_repo, runner_b_common):
        path.mkdir(parents=True)

    claimed_a = claim_ports(
        repo_root=runner_a_repo,
        common_dir=runner_a_common,
        environ={PORT_LANE_ENV: "1"},
    )
    claimed_b = claim_ports(
        repo_root=runner_b_repo,
        common_dir=runner_b_common,
        environ={PORT_LANE_ENV: "2"},
    )

    assert {claimed_a.backend, claimed_a.frontend}.isdisjoint(
        {claimed_b.backend, claimed_b.frontend}
    ), f"lane 1 pair {claimed_a} overlaps lane 2 pair {claimed_b}"


def test_port_lanes_never_overlap_across_their_full_pool_window() -> None:
    """If any two distinct lanes are compared then their entire scan windows,
    not just the slot each currently happens to pick, must be disjoint.

    A registry can advance past slot 0 (a stale reservation, a bound port from
    something unrelated), so a guard that only checks the first slot can pass
    today and collide tomorrow when a runner's registry has non-trivial state.
    This also mutates in both directions: a lane that is ignored entirely (the
    pre-fix behavior) collapses every window onto lane 0 and fails immediately
    below; a stride narrower than the pool needs also fails, caught by
    asserting the FULL range rather than a single sampled point.
    """
    claimed_ports: set[int] = set()
    for lane in range(24):
        backend_start, frontend_start = _lane_pool_starts(lane)
        lane_ports = set(range(backend_start, backend_start + POOL_SIZE)) | set(
            range(frontend_start, frontend_start + POOL_SIZE)
        )
        overlap = claimed_ports & lane_ports
        assert not overlap, f"lane {lane} window overlaps an earlier lane at {sorted(overlap)}"
        claimed_ports |= lane_ports


def test_port_lane_zero_matches_the_original_unshifted_pool_bounds() -> None:
    """If no lane is configured then allocation must be byte-identical to before
    this feature existed, so the two pre-existing test files stay green."""
    assert _lane_pool_starts(0) == (BACKEND_POOL_START, FRONTEND_POOL_START)


def test_port_lane_rejects_a_negative_or_malformed_value(tmp_path: Path) -> None:
    """If MUSIC_DJ_PORT_LANE is negative or not an integer then claiming must
    fail loudly rather than silently wrapping into another lane's window."""
    repo_root = tmp_path / "repo-a"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    for bad_value in ("-1", "not-a-number", "1.5"):
        with pytest.raises(PortConfigError, match=PORT_LANE_ENV):
            claim_ports(
                repo_root=repo_root,
                common_dir=common_dir,
                environ={PORT_LANE_ENV: bad_value},
            )


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


def test_show_ports_names_a_missing_worktree_instead_of_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed build has no Git worktree; show_ports must refuse loudly, not
    let ``git``'s subprocess.CalledProcessError escape uncaught (issue #2786)."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    with pytest.raises(WorktreeUnavailableError) as exc_info:
        show_ports()
    assert isinstance(exc_info.value, PortConfigError)
    assert "not running inside a Git worktree" in str(exc_info.value)


def test_show_ports_names_a_missing_git_binary_instead_of_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packaged build may ship with no ``git`` executable at all; that must also
    surface as WorktreeUnavailableError, not a bare FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()

    with pytest.raises(WorktreeUnavailableError):
        show_ports()


pytestmark = pytest.mark.rb_parity


def test_a_reservation_from_another_lane_is_reallocated_not_restored(tmp_path: Path) -> None:
    """If a checkout's registry still holds a pair claimed before its lane existed
    then a claim under the lane must allocate inside the lane's window, not
    restore the stale pair.

    A self-hosted runner's checkout persists between jobs, `.git` and its
    `worktree-ports.json` included, so the first claim on that runner (made
    before #1304 gave it a lane, or by an older workflow) is restored verbatim
    by every later job: `claim_ports` prefers the registry's current pair over
    allocation. Sat 5 Sep 2026 18:00 UTC, agentbox-3 derived
    MUSIC_DJ_PORT_LANE=4 and still claimed 8680/9400, the lane-0 base pair,
    and collided with the runner next to it (run 33982697725). The lane is a
    window; a remembered pair outside it is another lane's pair.
    """
    repo_root = tmp_path / "runner-3" / "_work" / "music-dj-tools"
    common_dir = tmp_path / "runner-3" / "_common"
    for path in (repo_root, common_dir):
        path.mkdir(parents=True)

    stale = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    assert port_config._pair_in_lane_window(stale, 0), stale
    # The persisted .env is what the post-checkout clean removes; the
    # registry under .git is what survives.
    (repo_root / ".env").unlink()

    relaid = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={PORT_LANE_ENV: "4"})

    lane_backend_start = BACKEND_POOL_START + 4 * PORT_LANE_STRIDE
    lane_frontend_start = FRONTEND_POOL_START + 4 * PORT_LANE_STRIDE
    assert lane_backend_start <= relaid.backend < lane_backend_start + POOL_SIZE, relaid
    assert lane_frontend_start <= relaid.frontend < lane_frontend_start + POOL_SIZE, relaid
    assert relaid != stale

    # CONTROL: inside its own lane a remembered pair IS restored, so the
    # single-claim-per-run contract the rest of the suite relies on holds.
    again = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={PORT_LANE_ENV: "4"})
    assert again == relaid


# -- issue #1613: fixed CI/desktop suite ports vs. the dynamic allocator -----
#
# A self-hosted e2e runner and this fleet's own worktrees share nucbox under
# different unix users and both allocate from the same ephemeral port range.
# Several e2e/desktop suites bind a FIXED, compile-time port that this
# module's registry never sees occupied, so an ordinary lane-0 dynamic claim
# (a developer machine, or a worker-fleet worktree with no MUSIC_DJ_PORT_LANE
# set) could legitimately allocate one of those exact ports.


@pytest.mark.requirement("INFRA-08")
def test_allocate_pair_never_returns_a_reserved_fixed_port() -> None:
    """[if] ports are allocated [then] no reserved fixed port is returned, [else stop].

    if the dynamic allocator hands a fixed CI/desktop suite's own port to
    a caller then broken (issue #1613)"""
    reserved_port = 8690
    assert reserved_port in RESERVED_FIXED_PORTS, "test premise: pick a real reserved port"
    reserved_slot = reserved_port - BACKEND_POOL_START

    # Every OTHER slot in the lane-0 window is already reserved by some other
    # worktree, so the reserved-fixed-port slot is the only one structurally
    # free. If it were handed out anyway, the allocator would return it here.
    reservations = {
        f"fake-worktree-{slot}": WebuiPorts(
            backend=BACKEND_POOL_START + slot, frontend=FRONTEND_POOL_START + slot
        )
        for slot in range(POOL_SIZE)
        if slot != reserved_slot
    }

    with pytest.raises(PortConfigError, match="no free worktree port pair"):
        _allocate_pair(reservations, lane=0)


def test_allocate_pair_falls_through_a_reserved_slot_to_the_next_free_one() -> None:
    """if excluding a fixed port's slot ALSO excludes the slot after it then
    broken -- the exclusion must be exact, not an off-by-one that eats
    capacity the fixed-port suites never claimed."""
    reserved_port = 8690
    reserved_slot = reserved_port - BACKEND_POOL_START
    free_slot = 50  # far from any RESERVED_FIXED_PORTS entry; see that set's own values
    assert BACKEND_POOL_START + free_slot not in RESERVED_FIXED_PORTS
    assert FRONTEND_POOL_START + free_slot not in RESERVED_FIXED_PORTS

    reservations = {
        f"fake-worktree-{slot}": WebuiPorts(
            backend=BACKEND_POOL_START + slot, frontend=FRONTEND_POOL_START + slot
        )
        for slot in range(POOL_SIZE)
        if slot not in (reserved_slot, free_slot)
    }

    selected = _allocate_pair(reservations, lane=0)

    assert selected == WebuiPorts(
        backend=BACKEND_POOL_START + free_slot, frontend=FRONTEND_POOL_START + free_slot
    )


@pytest.mark.requirement("INFRA-08")
def test_claim_ports_rejects_an_explicit_request_for_a_reserved_fixed_port(
    tmp_path: Path,
) -> None:
    """[if] a reserved port is requested [then] the claim is refused, [else stop].

    if an explicit --backend-port/.env request for a fixed suite's own port
    is honored then broken (issue #1613)"""
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    reserved_backend = next(iter(RESERVED_FIXED_PORTS))
    other_port = _free_port(exclude=(reserved_backend,))

    with pytest.raises(PortConfigError, match="reserved for a fixed-port"):
        claim_ports(
            repo_root=repo_root,
            common_dir=common_dir,
            environ={},
            requested=WebuiPorts(backend=reserved_backend, frontend=other_port),
        )


def test_claim_ports_writes_a_cross_uid_readable_ownership_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a claimed port has no ownership marker then a self-hosted CI runner
    on the same host, as a different unix user, cannot name the claimant and
    is back to reporting `<unreadable>` (issue #1613)"""
    monkeypatch.setattr(port_config, "PORT_OWNER_REGISTRY_DIR", tmp_path / "owners")
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    claimed = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

    note = describe_port_owner(claimed.backend)
    assert note is not None
    assert str(repo_root.resolve()) in note

    marker = next((tmp_path / "owners").glob(f"{claimed.backend}-*.owner"))
    # World-readable: a different unix user's process must be able to read
    # this file even though it cannot read this worktree's /proc entries.
    assert marker.stat().st_mode & 0o777 == 0o644


def test_release_ports_removes_its_ownership_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a released port keeps naming its old claimant then broken -- a
    freed port with a stale marker sends the next reader to the wrong place"""
    monkeypatch.setattr(port_config, "PORT_OWNER_REGISTRY_DIR", tmp_path / "owners")
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    claimed = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    assert describe_port_owner(claimed.backend) is not None

    released = release_ports(repo_root=repo_root, common_dir=common_dir)

    assert released is True
    assert describe_port_owner(claimed.backend) is None
    assert describe_port_owner(claimed.frontend) is None


@pytest.mark.requirement("INFRA-08")
def test_claim_ports_reallocates_a_pre_existing_now_reserved_env_pair(
    tmp_path: Path,
) -> None:
    """[if] .env names a now-reserved port [then] the pair is reallocated, [else stop].

    if a checkout's own .env already names a now-reserved fixed port, from
    before RESERVED_FIXED_PORTS existed, and claim_ports raises instead of
    discarding it and reallocating a safe pair, then broken -- the exact
    worktree issue #1613 describes could never restart without hand-editing
    .env, contrary to claim's own safe-to-rerun contract"""
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    reserved_backend = next(iter(RESERVED_FIXED_PORTS))
    other_port = _free_port(exclude=(reserved_backend,))
    _write_env(repo_root, reserved_backend, other_port)

    claimed = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

    assert claimed.backend not in RESERVED_FIXED_PORTS
    assert claimed.frontend not in RESERVED_FIXED_PORTS


def test_owner_registry_dir_is_the_shared_path_the_reaper_reads() -> None:
    """if the marker directory is derived from tempfile.gettempdir() (and so
    follows TMPDIR) while the reaper reads a fixed /tmp path then broken -- the
    marker is never found and the cross-uid holder goes unnamed (issue #1613).

    Read from the source rather than from the imported constant: a private
    TMPDIR that merely HAPPENS to be /tmp would satisfy the constant, and
    re-importing the module to vary TMPDIR would redefine ``PortConfigError``
    under every other test in this file.

    The derivation is REGENERATED here from the same expression rather than
    compared to a remembered literal, so a future edit that reintroduces
    ``tempfile.gettempdir()`` changes the derived value and reds this.
    """
    source = (PROJECT_ROOT / "apps" / "webui" / "port_config.py").read_text(encoding="utf-8")
    tmpdir_derivation = "PORT_OWNER_REGISTRY_DIR = Path(tempfile.gettempdir())"
    assert tmpdir_derivation not in source, (
        "PORT_OWNER_REGISTRY_DIR follows TMPDIR again; scripts/ci_reap_port_holders.sh "
        "reads a fixed path and would never find the markers"
    )
    registry_dir = port_config.PORT_OWNER_REGISTRY_DIR
    assert registry_dir == Path("/tmp/music-dj-tools-port-owners")

    script = (PROJECT_ROOT / "scripts" / "ci_reap_port_holders.sh").read_text(encoding="utf-8")
    assert (
        f"${{MDT_CI_PORT_OWNER_REGISTRY_DIR:-{port_config.PORT_OWNER_REGISTRY_DIR}}}"
    ) in script, "the reaper's default marker directory has drifted from PORT_OWNER_REGISTRY_DIR"


def test_the_tmpdir_probe_would_notice_a_gettempdir_derivation(tmp_path: Path) -> None:
    """CONTROL: the assertion above is a literal comparison, so prove the
    TMPDIR-dependence it is standing in for is real and observable -- with
    TMPDIR pointed elsewhere, the derivation it forbids produces a DIFFERENT
    path, which is exactly the divergence that leaves a marker unread."""
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    with (
        mock.patch.dict(os.environ, {"TMPDIR": str(private_tmp)}),
        # tempfile caches its answer in module state after the first call, so
        # the cache is cleared for the probe to read the environment at all.
        mock.patch.object(tempfile, "tempdir", None),
    ):
        derived = Path(tempfile.gettempdir()) / "music-dj-tools-port-owners"
    assert derived != Path("/tmp/music-dj-tools-port-owners"), (
        "TMPDIR did not move tempfile.gettempdir() here, so this control cannot fail "
        "for the reason under test and the probe above proves nothing"
    )


def test_claim_ports_refuses_a_reserved_port_inherited_from_the_environment(
    tmp_path: Path,
) -> None:
    """if an exported MUSIC_DJ_BACKEND_PORT names a now-reserved fixed port and
    claim_ports silently reallocates into .env instead of refusing then broken
    -- the exported value outranks the file just rewritten, so every later
    read resolves the reserved port again and fails ownership validation"""
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()
    reserved_backend = next(iter(RESERVED_FIXED_PORTS))
    other_port = _free_port(exclude=(reserved_backend,))

    with pytest.raises(PortConfigError, match=str(reserved_backend)):
        claim_ports(
            repo_root=repo_root,
            common_dir=common_dir,
            environ={BACKEND_ENV: str(reserved_backend), FRONTEND_ENV: str(other_port)},
        )


def test_claim_ports_reallocates_a_registry_pair_that_becomes_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if the registry's own remembered pair for this worktree is later added
    to RESERVED_FIXED_PORTS (a suite renumbering onto what used to be free
    dynamic space) and claim_ports keeps restoring it anyway then broken --
    the same staleness as a checkout's own .env, recorded in the shared
    registry instead (issue #1613)"""
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    first = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    (repo_root / ".env").unlink()  # only the registry remembers it now
    monkeypatch.setattr(
        port_config, "RESERVED_FIXED_PORTS", frozenset({first.backend, first.frontend})
    )

    relaid = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

    assert relaid != first
    assert relaid.backend not in port_config.RESERVED_FIXED_PORTS
    assert relaid.frontend not in port_config.RESERVED_FIXED_PORTS


def test_write_owner_marker_tolerates_a_directory_it_cannot_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a second uid's claim fails outright because it cannot chmod a
    registry directory another uid already created then broken -- on the
    intended shared host, mode 1777 already lets every uid write there, and
    only the directory's owner may chmod it (issue #1613)"""
    registry_dir = tmp_path / "owners"
    registry_dir.mkdir()
    os.chmod(registry_dir, 0o1777)  # a real chmod, unlike mkdir(mode=...), ignores umask
    monkeypatch.setattr(port_config, "PORT_OWNER_REGISTRY_DIR", registry_dir)

    def _fake_chmod(path: object, mode: int) -> None:
        if path == registry_dir:
            raise PermissionError("not the owner")

    monkeypatch.setattr(port_config.os, "chmod", _fake_chmod)

    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    claimed = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})

    assert claimed.backend not in RESERVED_FIXED_PORTS  # the claim itself still succeeded


def test_write_owner_marker_reraises_a_chmod_failure_on_a_wrong_mode_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL for the test above: a chmod failure on a directory that is NOT
    already sticky-world-writable is a real permission problem, not the
    benign second-uid case, and must still surface"""
    registry_dir = tmp_path / "owners"
    registry_dir.mkdir(mode=0o700)
    monkeypatch.setattr(port_config, "PORT_OWNER_REGISTRY_DIR", registry_dir)

    def _fake_chmod(path: object, mode: int) -> None:
        if path == registry_dir:
            raise PermissionError("not the owner")

    monkeypatch.setattr(port_config.os, "chmod", _fake_chmod)

    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    with pytest.raises(PermissionError):
        claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})


def test_ownership_marker_functions_are_a_no_op_without_os_getuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if claim_ports/release_ports crash on a platform with no os.getuid
    (Windows) then broken -- the cross-uid marker scheme is specific to the
    Linux CI-host collision issue #1613 describes and must not break the
    repo's Windows webui dev flow, which previously used only portable
    Python APIs"""
    monkeypatch.setattr(port_config, "PORT_OWNER_REGISTRY_DIR", tmp_path / "owners")
    monkeypatch.delattr(port_config.os, "getuid", raising=False)
    repo_root = tmp_path / "repo"
    common_dir = tmp_path / "common"
    repo_root.mkdir()
    common_dir.mkdir()

    claimed = claim_ports(repo_root=repo_root, common_dir=common_dir, environ={})
    assert describe_port_owner(claimed.backend) is None  # no marker written, no crash either

    released = release_ports(repo_root=repo_root, common_dir=common_dir)
    assert released is True
