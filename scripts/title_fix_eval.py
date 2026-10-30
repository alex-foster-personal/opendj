"""Score the title auto-fixer against labelled fixture sets and sync labels with a Google Sheet.

    doppler run -p general -c stg_cloud -- uv run --locked python -m scripts.title_fix_eval
    ... -m scripts.title_fix_eval --set prior_art --set strat150   # pick fixture sets
    ... -m scripts.title_fix_eval --only-failing                   # rerun cached misses only
    ... -m scripts.title_fix_eval --ids s099,p02                   # rerun named rows only
    ... -m scripts.title_fix_eval --regex-only                     # offline, no OpenRouter
    ... -m scripts.title_fix_eval --sheet <spreadsheet-id>         # push results to the sheet
    ... -m scripts.title_fix_eval --pull <spreadsheet-id>          # pull the maintainer's labels into a CANDIDATE file
    ... -m scripts.title_fix_eval --accept-pulled                  # after reviewing the diff: promote it to cases.json

Fixture sets live in (private path)/<set>.json (or $MDT_TITLE_FIX_FIXTURES): gitignored rows of the
owner's own library, never committed. Every live run writes its rows to a per-set cache
(.tmp/title_fix_eval/<set>.json); --only-failing and --ids rerun a subset and
print BOTH the subset score and a merged score that names how many rows came from the cache
(cached rows were scored by older code, so the merged line is a hint, never a release number).

Statuses in labels: fixed, clean, review, not_music, project (a DJ project file, title kept) and
not_project (projects set only: any status except project/unknown passes). A case with
expected_original_artist (covers) must also match the cover's original artist. title_only
(regression_mb: AcoustID/MusicBrainz truth) scores the TITLE only, base form (version brackets,
feat. and case ignored); rows with mb_disputed (reviewed MB-side errors and out-of-scope typos)
are an allowlist: never a pass or a wrong-auto, counted as "allowlisted".

Per set it prints pass, auto-ok (passed AND auto-applied, no human needed), wrong auto-applies (status
fixed but output != label), unknown, project, safe-tier rows (no JEV/DeepSeek call), DeepSeek
calls, cost and wall time; then per-tag precision/recall over every row that has expected_tags.
Exits nonzero when any OpenRouter call failed (UNKNOWN never passes).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from apps.tags.http import (
    FIXER_CFG as CFG,
)
from apps.tags.http import (
    Track,
    Verdict,
    fix_tracks,
    needs_person_selection,
    project_rules,
    project_tags,
    regex_propose,
    safe_tier,
    verdict_row,
    with_tags,
)

ROOT = Path(__file__).resolve().parent.parent
# The labelled sets are rows of the owner's own library, which the public tree must not carry (licensing rule R14):
# they live in a gitignored private directory, never under tests/fixtures.
FIXTURES = Path(os.environ.get("MDT_TITLE_FIX_FIXTURES") or ROOT / "data" / "private" / "title_fix")
CACHE = ROOT / ".tmp" / "title_fix_eval"
SETS = ("cases", "strat150", "prior_art", "covers", "projects", "mashups", "regression_mb")
TRACK_FIELDS = ("folder", "album", "label", "year", "length_s", "bit_depth", "sample_rate", "bitrate")
CASES = FIXTURES / "cases.json"
MANIFEST = FIXTURES / "MANIFEST.json"
MANIFEST_VERSION = 1
CANDIDATE = FIXTURES / "cases.pulled.json"  # a pull lands here; only --accept-pulled touches the committed labels
TAB = "eval"
# Production auto-apply admits only `fixed` proposals (auto_apply_eligible); `clean` writes nothing and `project`,
# `review`, `not_music` and `unknown` are applied only by an explicit human selection, so only `fixed` can be a
# wrong or a successful AUTOMATIC write.
AUTO_WRITE_STATUSES = ("fixed",)
LABEL_COLS = ("expected_artist", "expected_title", "expected_status")


def _norm(text: str) -> str:
    """Punctuation-blind but case-sensitive, so a SHOUTED title is a miss."""
    return re.sub(r"\W+", "", text)


def _matches_label(case: dict[str, Any], artist: str, title: str) -> bool:
    return _norm(artist) == _norm(case["expected_artist"]) and _norm(title) == _norm(case["expected_title"])


_MB_VERSION = re.compile(
    r"\s+-\s+.*\b(?:mix|remix(?:ed)?|edit|version|dub|remaster(?:ed)?|vip|instrumental|rework|bootleg|live|extended"
    r"|original|radio|club|acapella|a cappella)\b.*$",
    re.I,
)


def _mb_fold(text: str) -> str:
    text = re.sub(r"^\s*the\s+", "", text.translate(str.maketrans({"\u00d8": "O", "\u00f8": "o"})), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def _mb_base(text: str) -> str:
    text = re.sub(r"\s*\b(?:feat\.?|ft\.?|featuring)\s.*$", "", re.sub(r"\s*[(\[][^)\]]*[)\]]", "", _MB_VERSION.sub("", text)), flags=re.I)
    return _mb_fold(text)


def title_agrees(got: str, truth: str) -> bool:
    """MusicBrainz title agreement: exact after folding, or the same base title (version text ignored)."""
    return _mb_fold(got) == _mb_fold(truth) or (bool(_mb_base(got)) and _mb_base(got) == _mb_base(truth))


def _mb_agrees(case: dict[str, Any], title: str) -> bool:
    return any(title_agrees(title, truth) for truth in (case["expected_title"], *case.get("mb_alternate_titles", ())))


def _passes(case: dict[str, Any], verdict: Verdict) -> bool:
    status, artist, title = verdict.status, verdict.artist, verdict.title
    if case["expected_status"] == "tags_only":
        return status != "unknown" and _scoped(case, verdict.tags) == _scoped(case, case["expected_tags"])
    if case["expected_status"] == "title_only":
        return not case.get("mb_disputed") and status not in ("not_music", "unknown", "project") and _mb_agrees(case, title)
    if case["expected_status"] == "not_project":
        return status not in ("project", "unknown")
    if case["expected_status"] in ("not_music", "project"):
        return status == case["expected_status"]
    if status in ("not_music", "unknown", "project"):
        return False
    if "expected_original_artist" in case and _norm(verdict.original_artist) != _norm(case["expected_original_artist"]):
        return False
    if case["expected_status"] == "review" and status != "review":
        return False
    return _matches_label(case, artist, title)


def _scoped(case: dict[str, Any], tags: tuple[str, ...] | list[str]) -> list[str]:
    """tag_scope limits tag scoring to the tags a set labels (mashups: only "mashup")."""
    scope = case.get("tag_scope")
    return sorted(t for t in tags if scope is None or t in scope)


def _auto_writes(case: dict[str, Any], track: Track, verdict: Verdict) -> bool:
    """Whether production would apply this verdict with no human selecting it (its own eligibility predicate)."""
    members = case.get("original_artists", [track.artist])
    return verdict.status in AUTO_WRITE_STATUSES and not needs_person_selection(
        producer_credit_removed=bool(verdict.producer),
        changes_artist=verdict.artist != track.artist,  # exact, as ProposalRow.changes_artist is
        original_member_count=len(members),
    )


def _wrong_auto_apply(
    case: dict[str, Any], artist: str, title: str, status: str, tags: tuple[str, ...] = (), *, auto_writes: bool = True,
) -> bool:
    """The costly failure: the pipeline would AUTOMATICALLY write something the label says is wrong."""
    if status not in AUTO_WRITE_STATUSES or not auto_writes:
        return False
    if case["expected_status"] == "review":  # the label says a human must look: any automatic write is the failure
        return True
    if case["expected_status"] == "tags_only":  # a spurious tag is written; a missed one is only a miss
        return bool(set(_scoped(case, tags)) - set(case["expected_tags"]))
    if case["expected_status"] == "title_only":
        return not case.get("mb_disputed") and not _mb_agrees(case, title)
    if case["expected_status"] == "not_project":
        return False
    return case["expected_status"] in ("not_music", "project") or not _matches_label(case, artist, title)


# ----------------------------------------------------------------- sheet


def _gws_executable() -> str:
    """The Google Workspace CLI by resolved path (never a bare name another program on PATH could shadow)."""
    resolved = shutil.which("gws")
    if resolved is None:
        raise SystemExit("[ERROR] the Google Workspace CLI (gws) is not on PATH; sheet pull/push needs it")
    return resolved


def _gws(*args: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    cmd = [_gws_executable(), "sheets", "spreadsheets", *args]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else {}


def push_sheet(sheet_id: str, rows: list[dict[str, Any]]) -> None:
    """Replace the eval tab without ever blanking it first: refuse when the sheet holds label edits this run would
    overwrite (pull them first), write the new values in one request, and only then clear the rows left below."""
    header = list(rows[0])
    values = [header] + [[str(r[h]) for h in header] for r in rows]
    _refuse_to_overwrite_sheet_labels(sheet_id, rows)
    params = json.dumps({"spreadsheetId": sheet_id, "range": f"{TAB}!A1", "valueInputOption": "RAW"})
    _gws("values", "update", "--params", params, body={"values": values})
    leftover = json.dumps({"spreadsheetId": sheet_id, "range": f"{TAB}!A{len(values) + 1}:ZZ"})
    _gws("values", "clear", "--params", leftover, body={})


def _refuse_to_overwrite_sheet_labels(sheet_id: str, rows: list[dict[str, Any]]) -> None:
    current = _gws("values", "get", "--params", json.dumps({"spreadsheetId": sheet_id, "range": TAB})).get("values")
    if not current:
        return
    header, body = current[0], current[1:]
    local_by_id = {str(r["id"]): r for r in rows}
    for sheet_row in body:
        cells = dict(zip(header, sheet_row + [""] * len(header), strict=False))
        local = local_by_id.get(cells.get("id", ""))
        if local is None:
            raise SystemExit(
                f"[ERROR] sheet row {cells.get('id')} is absent from this run (a partial run would delete it); "
                "push the full set, nothing written"
            )
        for col in LABEL_COLS:
            if cells.get(col, "") and cells[col] != str(local[col]):
                raise SystemExit(f"[ERROR] sheet label {col} of {cells['id']} differs from this run; --pull first, nothing written")


def pull_labels(sheet_id: str, *, cases_path: Path = CASES, candidate_path: Path = CANDIDATE) -> int:
    """Pull the sheet's labels into a disposable CANDIDATE copy, never the committed fixture: a stale or wrong sheet
    must not move the baseline the fixer is scored against. Returns the number of label edits in the candidate."""
    params = json.dumps({"spreadsheetId": sheet_id, "range": TAB})
    values = _gws("values", "get", "--params", params)["values"]
    header, body = values[0], values[1:]
    by_id = {row[header.index("id")]: dict(zip(header, row + [""] * len(header), strict=False)) for row in body}
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    changed = 0
    for case in cases:
        sheet_row = by_id.get(case["id"])
        if sheet_row is None:
            raise SystemExit(f"[ERROR] case {case['id']} missing from sheet; refusing a partial pull")
        for col in LABEL_COLS:
            if sheet_row[col] != case[col]:
                case[col], changed = sheet_row[col], changed + 1
    candidate_path.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return changed


def accept_pulled(*, cases_path: Path = CASES, candidate_path: Path = CANDIDATE) -> int:
    """Promote a reviewed candidate to the committed fixture. Refuses unless it holds exactly the same cases in the
    same order and differs only in the label columns. Returns the number of label edits promoted."""
    if not candidate_path.is_file():
        raise SystemExit(f"[ERROR] no candidate at {candidate_path}; run --pull first")
    committed = json.loads(cases_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if [c["id"] for c in committed] != [c["id"] for c in candidate]:
        raise SystemExit("[ERROR] candidate cases differ from the committed ids or order; refusing to promote")
    changed = 0
    for old, new in zip(committed, candidate, strict=True):
        other_cols = {k for k in old.keys() | new.keys() if k not in LABEL_COLS}
        if any(old.get(k) != new.get(k) for k in other_cols):
            raise SystemExit(f"[ERROR] candidate case {old['id']} changes a non-label column; refusing to promote")
        changed += sum(old.get(col) != new.get(col) for col in LABEL_COLS)
    cases_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    candidate_path.unlink()
    return changed


def accept_and_repin(committed_file: Path, candidate_file: Path, sets_dir: Path, pin_file: Path) -> int:
    """Promote the reviewed candidate and re-pin the manifest in one step, so the promoted fixture still verifies."""
    changed = accept_pulled(cases_path=committed_file, candidate_path=candidate_file)
    write_manifest(sets_dir, pin_file)
    return changed


# ----------------------------------------------------------------- fixture manifest


def _fixture_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(sets_dir: Path, pin_file: Path) -> int:
    """Pin the version and sha256 of every fixture set in sets_dir. Returns the number pinned."""
    pinned = {path.name: _fixture_sha256(path) for path in sorted(sets_dir.glob("*.json")) if path != pin_file and path.stem in SETS}
    manifest = {"version": MANIFEST_VERSION, "sha256": pinned}
    pin_file.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    return len(pinned)


def verify_fixture(path: Path, manifest_path: Path = MANIFEST) -> bytes:
    """Return the fixture's bytes only if the manifest pins them: unknown version, unpinned file or hash mismatch
    exits before anything is scored. The bytes returned are the bytes hashed, so a swap between check and use is moot."""
    if not manifest_path.is_file():
        raise SystemExit(f"[ERROR] fixture manifest missing at {manifest_path}; run --rewrite-manifest after reviewing the fixtures")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != MANIFEST_VERSION:
        raise SystemExit(f"[ERROR] fixture manifest version {manifest.get('version')!r} is not {MANIFEST_VERSION}")
    pinned = manifest["sha256"].get(path.name)
    if pinned is None:
        raise SystemExit(f"[ERROR] {path.name} is not pinned in {manifest_path.name}; refusing to score an unpinned fixture")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pinned:
        raise SystemExit(f"[ERROR] {path.name} sha256 {actual[:12]} != pinned {pinned[:12]}; refusing to score a changed fixture")
    return raw


# ----------------------------------------------------------------- scoring


def _load_set(name: str, manifest_path: Path = MANIFEST) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A set is a list of cases, or {"_meta": {...}, "cases": [...]}; _meta.dj_aliases is a synthetic
    owner alias list the set needs (exported as the per-user env var for that set only)."""
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"[ERROR] fixture set {name!r} not found at {path}; the owner-library sets are private (set MDT_TITLE_FIX_FIXTURES)")
    data = json.loads(verify_fixture(path, manifest_path).decode("utf-8"))
    if isinstance(data, list):
        return data, {}
    return data["cases"], data["_meta"]


def _track(case: dict[str, Any]) -> Track:
    extra = {f: case[f] for f in TRACK_FIELDS if f in case}
    return Track(case["id"], case["title"], case["artist"], case.get("filename", ""), **extra)


def _cache_path(name: str) -> Path:
    return CACHE / f"{name}.json"


def _read_cache(name: str) -> dict[str, dict[str, Any]]:
    path = _cache_path(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _regex_only_verdict(track: Track) -> Verdict:
    """Offline: strong project rules count as project (no JEV gate), review reasons as review; the safe tier
    and the sampler rule are the real no-API paths, so they are labelled safe-tier exactly as live."""
    proposal = regex_propose(track)
    strong, weak = project_rules(track)
    if strong:
        source = "safe-tier" if "sampler" in strong else "rules-only"
        return Verdict("project", track.artist, track.title, source, proposal, tags=project_tags(strong))
    status = "review" if proposal.review_reasons else "fixed"
    source = "safe-tier" if not weak and safe_tier(track, proposal) else "regex-only"
    return with_tags(Verdict(status, proposal.artist, proposal.title, source, proposal))


def _score_row(case: dict[str, Any], track: Track, verdict: Verdict) -> dict[str, Any]:
    passed = _passes(case, verdict)
    auto_writes = _auto_writes(case, track, verdict)
    wrong = _wrong_auto_apply(case, verdict.artist, verdict.title, verdict.status, verdict.tags, auto_writes=auto_writes)
    tags = _scoped(case, verdict.tags)
    want_tags = case.get("expected_tags")
    row = verdict_row(track, verdict)
    disputed = bool(case.get("mb_disputed"))
    return {
        **row,
        **{c: case[c] for c in LABEL_COLS},
        "mb_disputed": "Y" if disputed else "",
        "expected_original_artist": case.get("expected_original_artist", ""),
        "expected_tags": "" if want_tags is None else " ".join(_scoped(case, want_tags)),
        "scored_tags": " ".join(tags),
        "pass": "-" if disputed else ("Y" if passed else "N"),
        "auto_write": "Y" if auto_writes else "N",
        "wrong_auto_apply": "Y" if wrong else "N",
        "tag_pass": "" if want_tags is None else ("Y" if tags == _scoped(case, want_tags) else "N"),
        "cost_usd": round(verdict.cost_usd, 6),
    }


def _summary(rows: list[dict[str, Any]]) -> str:
    n = len(rows)
    tagged = [r for r in rows if r["tag_pass"]]
    tag_part = f"  tags {sum(r['tag_pass'] == 'Y' for r in tagged)}/{len(tagged)}" if tagged else ""
    auto_ok = sum(r["pass"] == "Y" and r["auto_write"] == "Y" for r in rows)
    allowlisted = sum(r["pass"] == "-" for r in rows)
    allow_part = f" ({allowlisted} allowlisted)" if allowlisted else ""
    safe = sum(r["source"] == "safe-tier" for r in rows)
    return (
        f"pass {sum(r['pass'] == 'Y' for r in rows)}/{n - allowlisted}{allow_part}  auto-ok {auto_ok}  "
        f"wrong-auto {sum(r['wrong_auto_apply'] == 'Y' for r in rows)}  "
        f"unknown {sum(r['status'] == 'unknown' for r in rows)}  project {sum(r['status'] == 'project' for r in rows)}"
        f"{tag_part}  safe-tier {safe} ({safe / n:.0%})  deepseek {sum(r['llm_called'] == 'Y' for r in rows)}  "
        f"cost ${sum(float(r['cost_usd']) for r in rows):.5f}"
    )


def tag_report(rows: list[dict[str, Any]]) -> list[str]:
    """Per-tag precision (fired AND labelled / fired) and recall over rows that carry expected_tags."""
    fired: Counter[str] = Counter()
    hit: Counter[str] = Counter()
    want: Counter[str] = Counter()
    for r in rows:
        if r["tag_pass"] == "":
            continue
        got, expected = set(r["scored_tags"].split()), set(r["expected_tags"].split())
        fired.update(got)
        hit.update(got & expected)
        want.update(expected)
    return [
        f"  {tag:22} precision {hit[tag]}/{fired[tag]}  recall {hit[tag]}/{want[tag]}"
        for tag in sorted(set(fired) | set(want))
    ]


def _print_miss(row: dict[str, Any]) -> None:
    flag = "WRONG-AUTO" if row["wrong_auto_apply"] == "Y" else "MISS"
    print(
        f"  [{flag}] {row['id']} {row['status']:9} {row['source']:16} got {row['final_artist']!r} / "
        f"{row['final_title']!r}  want {row['expected_artist']!r} / {row['expected_title']!r} "
        f"({row['expected_status']}, jev {row['jev_p_ok']}) {row['error']}"
    )


def _select(cases: list[dict[str, Any]], name: str, ids: set[str], only_failing: bool) -> list[dict[str, Any]]:
    if ids:
        return [c for c in cases if c["id"] in ids]
    if only_failing:
        cached = _read_cache(name)
        if not cached:
            raise SystemExit(f"[ERROR] --only-failing needs a cache for {name!r}; run the full set once first")
        return [
            c
            for c in cases
            if c["id"] not in cached or cached[c["id"]]["pass"] == "N" or cached[c["id"]]["wrong_auto_apply"] == "Y"
        ]
    return cases


def score_set(name: str, ids: set[str], only_failing: bool, regex_only: bool) -> list[dict[str, Any]]:
    cases, meta = _load_set(name)
    chosen = _select(cases, name, ids, only_failing)
    if not chosen:
        print(f"[{name}] nothing selected")
        return []
    tracks = [_track(c) for c in chosen]
    started = time.monotonic()
    saved_aliases = os.environ.pop(CFG.DJ_ALIASES_ENV, None)
    if meta.get("dj_aliases"):
        os.environ[CFG.DJ_ALIASES_ENV] = ",".join(meta["dj_aliases"])
    try:
        verdicts = [_regex_only_verdict(t) for t in tracks] if regex_only else fix_tracks(tracks)
    finally:
        os.environ.pop(CFG.DJ_ALIASES_ENV, None)
        if saved_aliases is not None:
            os.environ[CFG.DJ_ALIASES_ENV] = saved_aliases
    wall = time.monotonic() - started
    rows = [_score_row(c, t, v) for c, t, v in zip(chosen, tracks, verdicts, strict=True)]
    for row in rows:
        if row["pass"] == "N" or row["wrong_auto_apply"] == "Y":
            _print_miss(row)
    mode = "regex-only" if regex_only else "pipeline"
    print(f"[{name}] {mode} {_summary(rows)}  wall {wall:.1f}s")
    if regex_only:
        return rows
    cached = _read_cache(name)
    cached.update({r["id"]: r for r in rows})
    CACHE.mkdir(parents=True, exist_ok=True)
    _cache_path(name).write_text(json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    if len(chosen) < len(cases):
        merged = [cached[c["id"]] for c in cases if c["id"] in cached]
        print(
            f"[{name}] merged with cache ({len(merged) - len(rows)} cached rows from older runs, "
            f"{len(cases) - len(merged)} never run): {_summary(merged)}"
        )
    return rows


# ----------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", action="append", choices=SETS, help="fixture set(s); default all")
    parser.add_argument("--ids", default="", help="comma-separated row ids to rerun")
    parser.add_argument("--only-failing", action="store_true", help="rerun rows that missed in the cache")
    parser.add_argument("--regex-only", action="store_true", help="score regex proposals offline")
    parser.add_argument("--sheet", help="spreadsheet id to push results to")
    parser.add_argument("--csv", type=Path, help="also write the result rows to this CSV")
    parser.add_argument("--pull", help="spreadsheet id to pull labels from into a candidate file, then exit")
    parser.add_argument("--rewrite-manifest", action="store_true", help="re-pin the fixture sha256 manifest after a reviewed change, then exit")
    parser.add_argument("--accept-pulled", action="store_true", help="promote the reviewed pulled candidate to cases.json")
    args = parser.parse_args()
    if args.pull:
        print(f"[OK] pulled {pull_labels(args.pull)} label edits into {CANDIDATE.name}; review the diff, then --accept-pulled")
        return 0
    if args.accept_pulled:
        print(f"[OK] promoted {accept_and_repin(CASES, CANDIDATE, FIXTURES, MANIFEST)} label edits into {CASES.name} and re-pinned {MANIFEST.name}")
        return 0
    if args.rewrite_manifest:
        print(f"[OK] pinned {write_manifest(FIXTURES, MANIFEST)} fixture sets in {MANIFEST.name}")
        return 0

    ids = {i.strip() for i in args.ids.split(",") if i.strip()}
    rows: list[dict[str, Any]] = []
    for name in args.set or SETS:
        rows += score_set(name, ids, args.only_failing, args.regex_only)
    if not rows:
        raise SystemExit("[ERROR] no rows scored")
    if len(args.set or SETS) > 1:
        print(f"[all] {_summary(rows)}")
    print("[tags] per-tag precision / recall on rows with expected_tags:", *tag_report(rows), sep="\n")
    if args.csv:
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"[OK] wrote {len(rows)} rows to {args.csv}")
    if args.sheet:
        push_sheet(args.sheet, rows)
        print(f"[OK] pushed {len(rows)} rows to https://docs.google.com/spreadsheets/d/{args.sheet}")
    return 1 if any(r["status"] == "unknown" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
