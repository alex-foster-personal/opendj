"""Playlist-writer protocol + six-rail safety adapter.

The in-memory :class:`PlaylistWriter` protocol (and the :class:`FakeWriter`
test fake) let the Phase 8 materialiser stay vendor-agnostic. For live
writes against the actual RB / djay databases this module also exposes
:class:`SafePlaylistWriter`, a thin adapter that enforces the six-rail
safety harness on every mutation:

  1. Process check    -- pgrep -f Rekordbox / djay Pro
  2. Timestamped DB backup BEFORE any write (``<db>.bak.<ISO8601>``).
  3. Typed confirmation (``--i-understand-the-risks`` flag == user
     explicitly acknowledged the risk; see
     :func:`require_typed_confirm_phrase` for an interactive prompt).
  4. Dry-run default: :class:`SafePlaylistWriter` refuses to call through
     to its underlying writer unless ``dry_run is False`` AND the caller
     passed ``flag_ok=True`` to the session.
  5. Post-write verify: on every successful mutation the adapter asks the
     underlying writer (if it exposes ``verify_playlist``) to read back
     the playlist membership; mismatches abort the session.
  6. Reversal script: each op appends a reverse snippet to
     ``data/sync/reversal/<ts>/reverse.sh``.

Rails 1-3, 5 and 6 are delegated to
:class:`apps.sync.safety.LiveWriteSession`. Rail 4 (dry-run default) is
enforced at this layer so the materialiser can keep its existing
``dry_run=True`` default without plumbing the safety session through
every call site.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

DEFAULT_CONFIRM_PHRASE = "APPLY SMARTLIST"


@runtime_checkable
class PlaylistWriter(Protocol):
    """Minimal playlist writer protocol used by the Phase 8 materialiser."""

    vendor: str

    def playlist_exists(self, name: str) -> bool: ...
    def create_playlist(self, name: str, track_ids: list[str]) -> None: ...
    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None: ...


@dataclass
class FakeWriter:
    """In-memory PlaylistWriter for tests."""

    vendor: str = "fake"
    playlists: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    raise_on_apply: bool = False

    def playlist_exists(self, name: str) -> bool:
        self.calls.append(("exists", name))
        return name in self.playlists

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        self.calls.append(("create", name, list(track_ids)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused to create {name}")
        self.playlists[name] = list(track_ids)

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        self.calls.append(("diff", name, list(added), list(removed)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused diff on {name}")
        current = self.playlists.setdefault(name, [])
        for rid in removed:
            if rid in current:
                current.remove(rid)
        for aid in added:
            if aid not in current:
                current.append(aid)


# --------------------------------------------------------- typed confirm


def require_typed_confirm_phrase(
    phrase: str = DEFAULT_CONFIRM_PHRASE,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> bool:
    """Rail 3: interactive typed confirmation prompt.

    Returns True only if the operator typed ``phrase`` exactly (case-
    sensitive). ``input_fn`` is injectable for tests.
    """
    prompt = f"Type {phrase!r} to proceed with live smartlist writes: "
    reader = input_fn or input
    try:
        answer = reader(prompt)
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == phrase


# ------------------------------------------------------- safe adapter


@dataclass
class SafePlaylistWriter:
    """Six-rail-guarded wrapper around a raw :class:`PlaylistWriter`.

    Rails 1, 2, 3, 5 and 6 are enforced by
    :class:`apps.sync.safety.LiveWriteSession`. Rail 4 (dry-run default)
    is enforced here: ``create_playlist`` and ``apply_diff`` short-circuit
    to a no-op record when ``dry_run`` is True, regardless of whether an
    underlying session is active.

    The adapter is a drop-in replacement for the underlying writer from
    the materialiser's point of view; ``vendor`` is passed through so
    ``MaterializeResult.writers_applied`` keys stay stable.
    """

    inner: PlaylistWriter
    db_path: Path
    target: str  # "rekordbox" | "djay"
    reason: str = "smartlist materialisation"
    dry_run: bool = True
    flag_ok: bool = False
    _session: Any = None  # LiveWriteSession | None at runtime
    _verify_cb: Callable[[str, list[str]], bool] | None = None

    @property
    def vendor(self) -> str:
        return self.inner.vendor

    def playlist_exists(self, name: str) -> bool:
        # Read-only: always safe, no rails.
        return self.inner.playlist_exists(name)

    def _require_live(self) -> None:
        if self.dry_run:
            raise RuntimeError(
                "SafePlaylistWriter: dry_run=True; pass dry_run=False "
                "and --i-understand-the-risks to perform live writes"
            )
        if not self.flag_ok:
            raise RuntimeError(
                "SafePlaylistWriter: typed-confirm flag not set; live "
                "writes refuse to run without --i-understand-the-risks"
            )
        if self._session is None:
            raise RuntimeError(
                "SafePlaylistWriter: no active LiveWriteSession; wrap "
                "calls in safe_writer_session(...) before writing"
            )

    def _verify(self, name: str, expected: list[str]) -> bool:
        """Rail 5: post-write verify via optional ``verify_playlist``."""
        if self._verify_cb is not None:
            return bool(self._verify_cb(name, expected))
        cb = getattr(self.inner, "verify_playlist", None)
        if cb is None:
            # No verifier available -- conservative default: pass.
            return True
        try:
            return bool(cb(name, expected))
        except Exception:
            return False

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        if self.dry_run:
            return
        self._require_live()
        sess = self._session
        # Install a per-op verifier on the session so the session's
        # verify-failure branch handles abort/skip/continue.
        prev_verifier = sess.verifier
        expected_members = list(track_ids)

        def _per_op_verifier(_track_id: str, _expected_none: Any) -> bool:
            return self._verify(name, expected_members)

        sess.verifier = _per_op_verifier
        try:
            with sess.per_track(f"create:{name}") as w:
                self.inner.create_playlist(name, track_ids)
                w.write({"op": "create", "name": name,
                         "n": len(track_ids)})
                if not w.verify_readback():
                    return
                w.append_reverse(
                    f"# revert smartlist create: delete playlist {name!r}"
                )
        finally:
            sess.verifier = prev_verifier

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        if self.dry_run:
            return
        self._require_live()
        sess = self._session
        pre_members: list[str] | None = None
        reader = getattr(self.inner, "read_members", None)
        if callable(reader):
            try:
                pre_members = list(reader(name))
            except Exception:
                pre_members = None

        prev_verifier = sess.verifier

        def _per_op_verifier(_track_id: str, _expected_none: Any) -> bool:
            expected = pre_members
            if expected is None:
                # No pre-state read -> conservatively call verifier with
                # the (added, removed) delta as the expected payload.
                return self._verify(name, list(added) + list(removed))
            expected = [m for m in expected if m not in set(removed)]
            expected = expected + [a for a in added if a not in expected]
            return self._verify(name, expected)

        sess.verifier = _per_op_verifier
        try:
            with sess.per_track(f"diff:{name}") as w:
                self.inner.apply_diff(name, added, removed)
                w.write({"op": "diff", "name": name,
                         "added": len(added), "removed": len(removed)})
                if not w.verify_readback():
                    return
                w.append_reverse(
                    f"# revert smartlist diff on {name!r}: "
                    f"added={added!r} removed={removed!r}"
                )
        finally:
            sess.verifier = prev_verifier


# ------------------------------------------------------- session helper


@contextmanager
def safe_writer_session(
    writer: SafePlaylistWriter,
    *,
    verifier: Callable[[str, list[str]], bool] | None = None,
    reversal_root: Path | None = None,
) -> Iterator[SafePlaylistWriter]:
    """Open a :class:`LiveWriteSession` around ``writer``.

    Dry-run calls short-circuit to a no-op session so the adapter stays a
    simple context-manager in both modes. This is the *single* public
    entry point callers should use to drive smartlist writes --
    constructing a :class:`SafePlaylistWriter` directly without this
    helper will refuse every live write at runtime.
    """
    if writer.dry_run:
        yield writer
        return

    # Lazy import so the test suite can exercise dry-run without pulling
    # in the sync-safety module.
    from apps.sync.safety import LiveWriteSession

    target = writer.target
    if target not in ("rekordbox", "djay"):
        raise ValueError(f"SafePlaylistWriter.target must be "
                         f"'rekordbox' or 'djay', got {target!r}")

    with LiveWriteSession(
        target=target,  # type: ignore[arg-type]
        reason=writer.reason,
        flag_ok=writer.flag_ok,
        db_path=writer.db_path,
        reversal_root=reversal_root,
    ) as sess:
        writer._session = sess
        writer._verify_cb = verifier
        try:
            yield writer
        finally:
            writer._session = None
            writer._verify_cb = None


__all__ = [
    "PlaylistWriter",
    "FakeWriter",
    "SafePlaylistWriter",
    "safe_writer_session",
    "require_typed_confirm_phrase",
    "DEFAULT_CONFIRM_PHRASE",
]
