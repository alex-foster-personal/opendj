"""Acceptance tests for REDTEAM-03 finding persistence."""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

import pytest

from scripts import redteam_findings as mod

SHA = "a" * 40


def _details(
    *, host: str, trace_session: str, trace_url: str, screenshot: str | None
) -> mod.FindingDetails:
    return mod.FindingDetails(
        fingerprint="deck-1-audio-output",
        sha=SHA,
        surface="area:performance deck output selection",
        host=host,
        repro=mod.Reproduction(
            act_sequence=("open /performance", "select deck 1 output"),
            trace_session=trace_session,
        ),
        evidence=mod.Evidence(trace_url=trace_url, screenshot=screenshot),
        honest_coverage=(
            "Output selection was not exercised because the host exposed no audio devices."
        ),
    )


def _a_second_finding() -> mod.Finding:
    """A finding at a DIFFERENT commit, so immutability does not refuse it as a rerun."""
    details = _details(
        host="bifrost2",
        trace_session="trace-session-847",
        trace_url="https://traces.example.test/847",
        screenshot=None,
    )
    return mod.Finding.fail(
        details=mod.FindingDetails(
            fingerprint=details.fingerprint,
            sha="b" * 40,
            surface=details.surface,
            host=details.host,
            repro=details.repro,
            evidence=details.evidence,
            honest_coverage=details.honest_coverage,
        )
    )


def _unavailable_finding() -> mod.Finding:
    return mod.Finding.unavailable(
        details=_details(
            host="agentbox-01",
            trace_session="trace-session-846",
            trace_url="https://traces.example.test/846",
            screenshot=None,
        ),
    )


def test_unreachable_surface_emits_unavailable_not_pass(tmp_path) -> None:
    """If a pod cannot reach its assigned surface then UNAVAILABLE is recorded, or broken."""
    store = mod.FindingStore(tmp_path / "index.jsonl")

    finding = _unavailable_finding()
    store.record(finding)

    [recorded] = [json.loads(line) for line in (tmp_path / "index.jsonl").read_text().splitlines()]
    assert recorded["verdict"] == "UNAVAILABLE"
    assert set(recorded) == {
        "fingerprint",
        "verdict",
        "sha",
        "surface",
        "host",
        "repro",
        "evidence",
        "honest_coverage",
    }
    assert recorded["evidence"] == {
        "trace_url": "https://traces.example.test/846",
        "screenshot": None,
    }
    assert store.read_all() == (finding,)

    details = _details(
        host="agentbox-01",
        trace_session="trace-session-pass",
        trace_url="https://traces.example.test/pass",
        screenshot=None,
    )
    with pytest.raises(TypeError, match="FAIL or UNAVAILABLE"):
        mod.Finding(
            fingerprint=details.fingerprint,
            verdict="PASS",  # type: ignore[arg-type]
            sha=details.sha,
            surface=details.surface,
            host=details.host,
            repro=details.repro,
            evidence=details.evidence,
            honest_coverage=details.honest_coverage,
        )


def test_missing_findings_directory_is_an_operational_error(tmp_path: Path) -> None:
    """If a pod's findings directory is missing then reading fails explicitly, or broken."""
    store = mod.FindingStore(tmp_path / "missing" / "index.jsonl")

    with pytest.raises(RuntimeError, match="finding index parent does not exist"):
        store.read_all()


def test_fail_record_has_exactly_the_documented_eight_fields() -> None:
    """If a surface fails then its record has no undocumented null ninth field, or broken."""
    finding = mod.Finding.fail(
        details=_details(
            host="agentbox-01",
            trace_session="trace-session-fail",
            trace_url="https://traces.example.test/fail",
            screenshot=None,
        )
    )

    assert set(finding.to_dict()) == {
        "fingerprint",
        "verdict",
        "sha",
        "surface",
        "host",
        "repro",
        "evidence",
        "honest_coverage",
    }


def test_later_pod_cannot_overwrite_existing_fingerprint_and_sha(tmp_path) -> None:
    """If a later pod reruns a case then its original verdict survives, or broken."""
    store = mod.FindingStore(tmp_path / "index.jsonl")
    original = mod.Finding.fail(
        details=_details(
            host="agentbox-01",
            trace_session="trace-session-first",
            trace_url="https://traces.example.test/first",
            screenshot="https://screenshots.example.test/first.png",
        ),
    )
    store.record(original)

    with pytest.raises(mod.ExistingFindingError, match="deck-1-audio-output"):
        store.record(_unavailable_finding())

    [recorded] = store.read_all()
    assert recorded.verdict is mod.Verdict.FAIL
    assert recorded.host == "agentbox-01"
    assert recorded.repro.trace_session == "trace-session-first"


@pytest.mark.parametrize(
    ("record_path", "extra_key"),
    [
        ((), "unexpected"),
        ((), "missing_capability"),
        (("repro",), "unexpected_repro"),
        (("evidence",), "unexpected_evidence"),
    ],
)
def test_finding_from_dict_rejects_unknown_record_fields(
    record_path: tuple[str, ...], extra_key: str
) -> None:
    """If a persisted record has an unknown field then loading rejects it, or broken."""
    record = json.loads(json.dumps(_unavailable_finding().to_dict()))
    target = record
    for key in record_path:
        target = target[key]  # type: ignore[assignment,index]
    target[extra_key] = "untrusted"  # type: ignore[index]

    with pytest.raises(ValueError, match=rf"unknown .* field.*{extra_key}"):
        mod.Finding.from_dict(record)


def test_finding_store_has_no_unconditional_posix_lock_import() -> None:
    """If bifrost2 imports the finding store then import succeeds without fcntl, or broken."""
    module_imports = [
        alias.name
        for node in ast.parse(Path(mod.__file__).read_text()).body
        if isinstance(node, ast.Import)
        for alias in node.names
    ]

    assert "fcntl" not in module_imports, (
        "fcntl is Unix-only and REDTEAM-02 runs pods on the Windows surface. "
        "Branch the import on sys.platform instead."
    )
    assert "msvcrt" not in module_imports, "and msvcrt is the same trap in reverse"
    assert "json" in module_imports, "the source probe must see normal module imports"


@pytest.mark.parametrize("nested_field", ["repro", "evidence"])
def test_malformed_nested_objects_are_rejected_before_persistence(
    tmp_path: Path, nested_field: str
) -> None:
    """If a nested finding object is malformed then the ledger is unchanged, or broken.

    Driven through the PERSISTENCE boundary, not just the constructor. The
    earlier form took `tmp_path`, never handed it to a `FindingStore`, and then
    asserted `index.jsonl` did not exist - an assertion nothing in the subject
    under test could ever falsify, because no persistence path ran. Sol found
    that on #1259.

    So: a real store with a real record already in it, the malformed write
    attempted against that store, and then the two things a later pod actually
    depends on - the file is byte-for-byte unchanged, and `read_all` still
    parses. The second matters because a malformed append does not only lose
    its own record: `_read_records` validates every line, so one bad line makes
    every subsequent read raise for every pod on the run.
    """
    index = tmp_path / "index.jsonl"
    store = mod.FindingStore(index)
    store.record(_unavailable_finding())
    before = index.read_bytes()
    assert before, "the ledger must have something to lose before we assert it kept it"

    details = _details(
        host="agentbox-01",
        trace_session="trace-session-invalid",
        trace_url="https://traces.example.test/invalid",
        screenshot=None,
    )
    values: dict[str, object] = {"repro": details.repro, "evidence": details.evidence}
    values[nested_field] = {"untrusted": "object"}

    with pytest.raises(TypeError, match=f"{nested_field} must be"):
        store.record(
            mod.Finding(
                fingerprint="deck-2-malformed",
                verdict=mod.Verdict.FAIL,
                sha=details.sha,
                surface=details.surface,
                host=details.host,
                repro=values["repro"],  # type: ignore[arg-type]
                evidence=values["evidence"],  # type: ignore[arg-type]
                honest_coverage=details.honest_coverage,
            )
        )

    assert index.read_bytes() == before, "a refused finding must not touch the ledger"
    assert len(store.read_all()) == 1, "and the ledger must still be readable by the next pod"


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch, by definition")
def test_the_index_lock_is_real_and_is_released(tmp_path: Path) -> None:
    """If the index lock stopped locking then this control fails, or broken.

    CONTROL on `test_finding_store_has_no_unconditional_posix_lock_import`. That
    test reads the source for a Unix-only module-scope import, and deleting the
    locking outright would satisfy it just as well as fixing it - absence of
    `fcntl` is exactly what a no-op looks like. So take the real lock on this
    platform, prove the kernel refuses a SECOND holder, and prove the lock is
    given back: a lock that is never released would hang the next pod on this
    host forever, and no other test in this file would notice.

    The refusal comes from the production primitive on a real file, not from a
    hand-built errno.
    """
    import fcntl  # POSIX-only, and this test is skipped elsewhere

    store = mod.FindingStore(tmp_path / "index.jsonl")
    lock_path = tmp_path / "index.jsonl.lock"

    with store._lock_index():
        assert lock_path.exists(), "the lock must be taken on a real sidecar file"
        with lock_path.open("a+b") as rival, pytest.raises(OSError):
            fcntl.flock(rival.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    with lock_path.open("a+b") as after:
        fcntl.flock(after.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(after.fileno(), fcntl.LOCK_UN)


def test_recording_still_goes_through_the_index_lock(tmp_path: Path) -> None:
    """If record stops holding the index lock then concurrent pods can interleave, or broken."""
    index = tmp_path / "index.jsonl"
    store = mod.FindingStore(index)
    store.record(_unavailable_finding())

    assert (tmp_path / "index.jsonl.lock").exists(), "record must go through the sidecar lock"
    assert len(store.read_all()) == 1
    assert json.loads(index.read_text(encoding="utf-8").strip())["verdict"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repro", "open /performance"),
        ("evidence", "https://traces.example.test/846"),
    ],
)
def test_a_nested_field_given_as_a_bare_string_is_refused(field: str, value: str) -> None:
    """If a nested field is a bare string then construction refuses it, or broken.

    The dict case is covered above. A STRING is the nastier input of the two:
    `Reproduction` and `Evidence` are both frozen dataclasses of strings, so a
    caller who flattens one by hand produces this, and `asdict` would have
    copied it to the ledger verbatim just the same.
    """
    good = _details(
        host="bifrost2",
        trace_session="trace-session-846",
        trace_url="https://traces.example.test/846",
        screenshot=None,
    )
    fields = {
        "fingerprint": good.fingerprint,
        "sha": good.sha,
        "surface": good.surface,
        "host": good.host,
        "repro": good.repro,
        "evidence": good.evidence,
        "honest_coverage": good.honest_coverage,
    }
    fields[field] = value

    with pytest.raises(TypeError, match=f"{field} must be"):
        mod.FindingDetails(**fields)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=f"{field} must be"):
        mod.Finding(verdict=mod.Verdict.FAIL, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["fingerprint", "sha", "surface", "host", "honest_coverage"])
def test_scalar_finding_fields_must_be_strings(field: str) -> None:
    """If a scalar finding field is not a string then construction refuses it, or broken.

    One layer below the nested-object checks. `fingerprint` was only ever
    `.strip()`ed, so a non-string raised AttributeError instead of a typed
    refusal naming the field, and `sha` leaned on `re.fullmatch` to raise for
    it. Both are accidents of implementation, not a contract a pod can rely on.
    """
    good = _details(
        host="bifrost2",
        trace_session="trace-session-846",
        trace_url="https://traces.example.test/846",
        screenshot=None,
    )
    fields = {
        "fingerprint": good.fingerprint,
        "sha": good.sha,
        "surface": good.surface,
        "host": good.host,
        "repro": good.repro,
        "evidence": good.evidence,
        "honest_coverage": good.honest_coverage,
    }
    fields[field] = 12345

    with pytest.raises(TypeError, match=f"{field} must be a string"):
        mod.FindingDetails(**fields)  # type: ignore[arg-type]


def test_a_missing_run_directory_is_an_error_not_an_empty_store(tmp_path: Path) -> None:
    """If the run directory is missing then reading raises, or broken.

    A mistyped, deleted or unmounted run directory used to read back as zero
    findings, which is exactly what a clean run looks like - REDTEAM-04 would
    dedupe and file against it and mask the operational failure. `record`
    already raised for this; `read_all` did not. Sol found the asymmetry on
    #1259.
    """
    missing = tmp_path / "never-created" / "index.jsonl"
    store = mod.FindingStore(missing)

    with pytest.raises(RuntimeError, match="finding index parent does not exist"):
        store.read_all()
    with pytest.raises(RuntimeError, match="finding index parent does not exist"):
        store.record(_unavailable_finding())

    # CONTROL: a real directory with no index yet is the honest empty case and
    # must still read as empty, or the check above would just be "always raise".
    assert mod.FindingStore(tmp_path / "index.jsonl").read_all() == ()


def test_the_windows_lock_retries_rather_than_dropping_a_finding() -> None:
    """If the Windows lock stops retrying then a contended write is lost, or broken.

    `msvcrt.locking(LK_LOCK)` retries ten times, one second apart, and then
    raises - so a Windows pod would DROP a valid FAIL whenever another pod held
    the sidecar for ten seconds, which grows likely as the ledger grows because
    every writer parses the whole file while holding the lock. Sol found it on
    #1259.

    This is a SOURCE check and cannot run the Windows branch from macOS, where
    `msvcrt` does not exist. What it pins is the shape: the non-blocking
    spelling inside a loop with a deadline, rather than the ten-try constant.
    Live win32 execution is unverified on this lane and is called out as such
    in the PR thread.
    """
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "msvcrt.LK_LOCK" not in source, (
        "LK_LOCK gives up after ten tries and drops the finding. Poll LK_NBLCK "
        "against a deadline instead. (Matched on the qualified CALL, not the bare "
        "name, so the prose explaining why it was dropped does not trip this.)"
    )
    assert "msvcrt.LK_NBLCK" in source, "the non-blocking spelling is what a retry loop needs"
    assert mod._LOCK_TIMEOUT_S >= 60, (
        "the deadline must outlast ordinary contention, or it is LK_LOCK with extra steps"
    )
    assert issubclass(mod.LockUnavailable, RuntimeError), (
        "exhausting the deadline must raise a NAMED error, so a lost lock is never "
        "mistaken for a run that found nothing"
    )


@pytest.mark.parametrize("length", [41, 47, 63])
def test_a_sha_of_an_impossible_length_is_refused(length: int) -> None:
    """If a SHA is neither 40 nor 64 hex then the finding is refused, or broken.

    A Git object ID is 40 lowercase hex (SHA-1) or 64 (SHA-256). Nothing in
    between exists, but `[0-9a-f]{40,64}` accepted all of it - and a recorded
    verdict is immutable, so one truncated SHA permanently mints a dedupe key
    REDTEAM-04 can never match against a real commit. Sol found it on #1259.
    """
    with pytest.raises(ValueError, match="sha must be"):
        mod.FindingDetails(
            fingerprint="deck-1-audio-output",
            sha="a" * length,
            surface="area:performance deck output selection",
            host="bifrost2",
            repro=mod.Reproduction(act_sequence=("open /performance",), trace_session="t-846"),
            evidence=mod.Evidence(trace_url="https://traces.example.test/846", screenshot=None),
            honest_coverage="unreachable",
        )


@pytest.mark.parametrize("length", [40, 64])
def test_both_real_git_object_id_lengths_are_accepted(length: int) -> None:
    """If a valid SHA-1 or SHA-256 id is refused then the tightening overshot, or broken.

    CONTROL on the test above. Narrowing a regex is exactly the change that
    quietly rejects legitimate input, and REDTEAM-01 hands pods whatever the
    repo's object format produces.
    """
    details = mod.FindingDetails(
        fingerprint="deck-1-audio-output",
        sha="a" * length,
        surface="area:performance deck output selection",
        host="bifrost2",
        repro=mod.Reproduction(act_sequence=("open /performance",), trace_session="t-846"),
        evidence=mod.Evidence(trace_url="https://traces.example.test/846", screenshot=None),
        honest_coverage="unreachable",
    )
    assert details.sha == "a" * length


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch, by definition")
def test_a_wedged_posix_lock_holder_ends_at_the_deadline(tmp_path: Path, monkeypatch) -> None:
    """If a POSIX lock holder wedges then the wait ends at the deadline, or broken.

    `flock(LOCK_EX)` blocks in the kernel forever, so one wedged holder stalled
    every reader and writer on the host with no error - while this module's
    docstring claimed bounded, fail-fast locking, which only the Windows half
    delivered. Sol found the contradiction on #1259.

    The wedge is REAL: a second open handle holds the sidecar's lock for the
    whole test, and the store must give up rather than hang. The deadline is
    shortened to keep the test fast; shortening it is the only thing patched,
    and the lock, the contention and the refusal all come from the kernel.
    """
    import fcntl  # POSIX-only, and this test is skipped elsewhere

    monkeypatch.setattr(mod, "_LOCK_TIMEOUT_S", 0.3)
    index = tmp_path / "index.jsonl"
    store = mod.FindingStore(index)
    store.record(_unavailable_finding())

    with (tmp_path / "index.jsonl.lock").open("a+b") as wedged:
        fcntl.flock(wedged.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(mod.LockUnavailable, match="held"):
            store.read_all()
        waited = time.monotonic() - started

    assert waited < 5, (
        f"the wait must end at the deadline, not block forever (waited {waited:.2f}s)"
    )

    # CONTROL: once the wedge is gone the very same call must succeed, or the
    # test above would also pass against a store that could never lock at all.
    assert len(store.read_all()) == 1


def test_a_record_replaces_the_index_rather_than_appending_in_place(tmp_path: Path) -> None:
    """If a record is appended in place then a torn write can be observed, or broken.

    An in-place append can tear: a kill, an I/O error or a full disk part way
    through leaves a truncated final line, and `_read_records` validates EVERY
    line, so the next read rejects the whole ledger and loses every finding
    recorded before it. Sol found it on #1259.

    Torn writes cannot be produced on demand here, so this asserts the property
    that makes them impossible instead: the published index is a DIFFERENT
    inode each time, which is what `os.replace` does and what an append never
    does. That is a real observable of the production path, not a stand-in.
    """
    index = tmp_path / "index.jsonl"
    store = mod.FindingStore(index)
    store.record(_unavailable_finding())
    first_inode = index.stat().st_ino

    store.record(_a_second_finding())

    assert index.stat().st_ino != first_inode, (
        "the index must be REPLACED, not appended to - a same-inode write can tear"
    )
    assert len(store.read_all()) == 2, "and both records must survive the replacement"
    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")]
    assert not leftovers, f"the temp file must not outlive a successful write: {leftovers}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the directory mode this test relies on")
def test_a_failed_write_leaves_the_previous_ledger_intact(tmp_path: Path) -> None:
    """If a write fails then the previous ledger is unchanged and still readable, or broken.

    The failure is REAL, not simulated: the run directory is made read-only, so
    the temp file the writer needs genuinely cannot be created and the OSError
    comes from the kernel. Nothing is patched.

    This is the half the inode check above cannot reach. That one proves the
    happy path publishes atomically; this one proves the SAD path publishes
    nothing - the earlier findings are still on disk, byte for byte, and still
    parse.
    """
    index = tmp_path / "index.jsonl"
    store = mod.FindingStore(index)
    store.record(_unavailable_finding())
    before = index.read_bytes()

    second = _a_second_finding()
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(OSError):
            store.record(second)
    finally:
        tmp_path.chmod(0o700)

    assert index.read_bytes() == before, "a failed write must not touch the published ledger"
    assert len(store.read_all()) == 1, "and what was there must still parse"

    # CONTROL: the same write must SUCCEED once the directory is writable, or
    # this test would also pass against a store that could never record at all.
    store.record(second)
    assert len(store.read_all()) == 2
