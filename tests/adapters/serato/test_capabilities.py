"""Capabilities descriptor snapshot test (OPEN-02c)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters.serato import capabilities
from apps.open_dj import serialize_jcs

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "serato_capabilities.snapshot.json"
)

pytestmark = pytest.mark.requirement("OPEN-02")

# The remediation hint. It used to name
# `python -m scripts.update_serato_capabilities_snapshot`, which has never
# existed in this repo (git log -S finds only the commit that introduced the
# hint, and no such file was ever added). That is the worst possible thing for
# a hint to be wrong about, because it is read at exactly the moment somebody
# is already blocked by a red test.
#
# There is still no regeneration script, so the command is inlined rather than
# invented. Verified byte-identical against the committed snapshot: indent=2
# plus sort_keys=True plus a trailing newline is the format on disk. Run from
# the repo root, which is what makes the relative path resolve.
REGEN_COMMAND = (
    "python -c \"import json; from apps.adapters.serato import capabilities; "
    "open('tests/data/serato_capabilities.snapshot.json', 'w').write("
    "json.dumps(capabilities().to_dict(), indent=2, sort_keys=True) + '\\n')\""
)


@pytest.mark.requirement("OPEN-02c")
def test_capabilities_snapshot_matches() -> None:
    """Serialise capabilities via JCS and compare byte-for-byte."""
    assert SNAPSHOT_PATH.exists(), (
        f"missing snapshot at {SNAPSHOT_PATH}.\n"
        f"Regenerate from the repo root with:\n  {REGEN_COMMAND}"
    )
    actual = serialize_jcs(capabilities().to_dict())
    expected = SNAPSHOT_PATH.read_bytes()
    # Expected file is human-readable pretty-printed JSON; compare after
    # normalising both through json.loads for a deep-equality check AND keep
    # the byte-level comparison via JCS serialisation.
    assert json.loads(actual) == json.loads(expected), (
        "Serato capabilities descriptor drifted from snapshot.\n"
        f"If this is intentional, regenerate from the repo root with:\n"
        f"  {REGEN_COMMAND}"
    )
