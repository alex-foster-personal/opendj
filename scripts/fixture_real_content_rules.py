"""Publish-gate rules for real library data in test fixtures (issue #11397, parent #5660).

Identity path shapes live in scripts/oss_tip_rules.py; this module holds the
commercial-title, captured-lyric, and real-library stable_id classes named in
(private path).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from scripts.oss_tip_rules import PATTERNS, RULE_PREFILTERS, _rule_accepts

LYRICS_ASR_PREFIX = "tests/fixtures/lyrics_asr/"
FRONTEND_UNIT_FIXTURES_PREFIX = "apps/webui/frontend/tests/unit/fixtures/"
TEST_SOURCE_SUFFIXES = (".py", ".mjs", ".ts", ".tsx", ".js", ".jsx")

# Captured stable ids from the owner's library (pre-GB-5 filenames).
REAL_LIBRARY_STABLE_IDS: frozenset[str] = frozenset(
    {
        "f70643404518d4af71a955ac001228896cf30e7f",
        "0c6fcf9a9dde29dd4ac71162e5b57e8024fcae10",
        "66b83341e26c955be715f46bf6c8f9f67722fb33",
        "daacfd89fb24c35f1c09d684b1a7d9aff67676f2",
        "07641ab99b9186184ae3a1df7a240055877d9eb7",
        "277aeba8a97fd3126d41fc544052da0890b48e12",
        "27d881a720290f21a01359dbf52375524a591967",
        "db3697a51a3acdfbf9a4bb254791443abca7dffa",
        "317fa699568b702ef4ea55625aa21b4ecb4cc403",
        "5f837bae3baceca9bcb2ad2402ad8f231eafcd5f",
        "6be9dac1e03272af23cf94ce3c8694dc8e4b1f34",
        "81866cbc29f5b088539aea327c19d03aac812f09",
        "9125cdd80e03030a67482f3eecb3a4fd981d0914",
        "992eda2577dc775bc35e8eea65370e9d04437f92",
        "a60fc4294160596643626c756565b0de1181ccb4",
        "a2ef5f098234381e1acca0c0c5f1bd7a8d2b1a21",
        "c1831c24c993eaf97bf9e80c3cd3a22aec691fb7",
        "f076808222a430574e8801fc2504bac5638839fd",
    }
)

BANNED_CAPTURED_LYRIC_FRAGMENTS: frozenset[str] = frozenset(
    {
        "I'm losing it",
        "From Gaza with love",
        "All I know, all I know, all I know",
        "Pullin', pullin', pullin', pullin'",
        "Came from Gaza with love",
    }
)

# Assembled from fragments so this module's own oss_tip scan stays quiet.
_COMMERCIAL_TOKENS: tuple[str, ...] = (
    "Strobe",
    "deadmau5",
    "personal jesus",
    "your own personal",
    "depeche",
    "Georgia",
    "Carmel",
)

COMMERCIAL_TRACK_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in _COMMERCIAL_TOKENS) + r")\b",
    re.IGNORECASE,
)

HOME_RULES = frozenset({"home-path", "windows-home-path", "linux-home-path"})


@dataclass(frozen=True)
class RealContentFinding:
    path: str
    line: int
    rule: str
    match: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.match}"


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _real_home_hits(path: str, text: str) -> list[tuple[int, str]]:
    lowered = text.lower()
    live = [
        (rule, pattern)
        for rule, pattern in PATTERNS
        if rule in HOME_RULES and any(t in lowered for t in RULE_PREFILTERS[rule])
    ]
    hits: list[tuple[int, str]] = []
    for rule, pattern in live:
        for match in pattern.finditer(text):
            preceding = text[max(0, match.start() - 256) : match.start()]
            if _rule_accepts(rule, match, preceding):
                hits.append((_line_no(text, match.start()), match.group(0)))
    return hits


def lyrics_asr_findings(path: str, text: str) -> list[RealContentFinding]:
    if not path.startswith(LYRICS_ASR_PREFIX) or not path.endswith(".json"):
        return []
    findings: list[RealContentFinding] = []
    rel_name = Path(path).name
    if rel_name.startswith("real_"):
        findings.append(RealContentFinding(path, 1, "fixture-lyrics-filename", rel_name))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return findings
    stable_id = payload.get("stable_id")
    if isinstance(stable_id, str) and stable_id in REAL_LIBRARY_STABLE_IDS:
        findings.append(RealContentFinding(path, 1, "fixture-lyrics-stable-id", stable_id))
    lines = payload.get("lines")
    if isinstance(lines, list):
        joined = "\n".join(str(line.get("text", "")) for line in lines if isinstance(line, dict))
        for fragment in BANNED_CAPTURED_LYRIC_FRAGMENTS:
            if fragment in joined:
                findings.append(RealContentFinding(path, 1, "fixture-lyrics-copyright", fragment))
    blob = json.dumps(payload).lower()
    for token in ("personal jesus", "your own personal", "depeche"):
        if token in blob:
            findings.append(RealContentFinding(path, 1, "fixture-lyrics-commercial-shape", token))
    return findings


def anlz_manifest_findings(path: str, text: str) -> list[RealContentFinding]:
    if not path.endswith(".json") or "manifest" not in Path(path).name:
        return []
    if not (path.startswith(FRONTEND_UNIT_FIXTURES_PREFIX) or path.startswith("tests/fixtures/")):
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    capture = payload.get("capture")
    if not isinstance(capture, dict):
        return []
    findings: list[RealContentFinding] = []
    for key in ("selection", "title", "artist"):
        value = capture.get(key)
        if isinstance(value, str):
            match = COMMERCIAL_TRACK_RE.search(value)
            if match:
                findings.append(RealContentFinding(path, 1, "fixture-anlz-commercial", f"{key}={match.group(0)}"))
    return findings


def _owner_track_scope(path: str) -> bool:
    suffix = Path(path).suffix
    if path.startswith("tests/"):
        if path.startswith("tests/fixtures/"):
            return False
        return suffix in TEST_SOURCE_SUFFIXES
    if path.startswith("apps/webui/frontend/tests/unit/fixtures/"):
        return suffix in TEST_SOURCE_SUFFIXES or suffix == ".json"
    if path.startswith("apps/webui/frontend/tests/"):
        return suffix in TEST_SOURCE_SUFFIXES
    return False


def test_source_owner_track_findings(path: str, text: str) -> list[RealContentFinding]:
    if not _owner_track_scope(path):
        return []
    commercial = COMMERCIAL_TRACK_RE.search(text)
    if not commercial:
        return []
    findings: list[RealContentFinding] = []
    for line_no, home in _real_home_hits(path, text):
        findings.append(
            RealContentFinding(
                path,
                line_no,
                "fixture-test-owner-track",
                f"{home} + {commercial.group(0)}",
            )
        )
    return findings


def fixture_real_content_findings(path: str, text: str) -> list[RealContentFinding]:
    return [
        *lyrics_asr_findings(path, text),
        *anlz_manifest_findings(path, text),
        *test_source_owner_track_findings(path, text),
    ]
