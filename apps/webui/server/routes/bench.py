"""Bench routes -- KPI ledger reads plus human rating persistence.

``scripts/bench/kpi_ledger.json`` is the canonical, machine-appended record of
farm runs (written by ``scripts/bench/kpi_append.py``). The KPI endpoint is the
only supported way for the app to read it: the browser never touches the file,
and an agent can curl the same JSON the UI renders.

The KPI ledger stays read-only here. Appending a snapshot is a CLI action
(``uv run scripts/bench/kpi_append.py --label <run> --set k=v --note "..."``)
so the ledger keeps one writer.

Ratings are different: ``scripts/bench/vocal_quality_rater.html`` produces one
JSON artifact per rating session, and until now the only copy lived in the
browser (localStorage + a manual download). POST /bench/ratings writes that
same artifact into ``scripts/bench/ratings/`` so the repo holds it. Agent
parity: the endpoint is plain JSON over HTTP, so a rating set can be posted,
listed and read back with curl, no browser involved.

Requirements (mini-PRD):
  ✔︎ ✅ GET  /bench/kpi:              the ledger verbatim (kpis + snapshots)
  ✔︎ ✅ POST /bench/ratings:          write one rating artifact, return its path
  ✔︎ ✅ GET  /bench/ratings:          list saved artifacts, newest first
  ✔︎ ✅ GET  /bench/ratings/{name}:   one artifact verbatim
Acceptance:
  [if] the ledger file is absent [then ⛔️] 503 naming the missing path
  [if] the ledger is not valid JSON [then ⛔️] 500 naming the parse error
  [if] the ledger lacks 'kpis' or 'snapshots' [then ⛔️] 500 naming the key
  [if] the ledger is well-formed [then] 200 with kpis + snapshots intact
  [if] a POSTed payload has no track, or no ratings, or a clip without an id
       [then ⛔️] 4xx and nothing is written to disk
  [if] every human_score in a POSTed payload is null [then ⛔️] 400: an empty
       rating set is never worth an artifact
  [if] a POST succeeds [then] the response names the exact relative path and
       that file parses back to the payload
  [if] a filename in GET /bench/ratings/{name} escapes the ratings dir
       [then ⛔️] 400, never a read outside scripts/bench/ratings
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/bench", tags=["bench"])

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Module-level so tests can monkeypatch to a tmp ledger / tmp ratings dir.
KPI_LEDGER_FILE: Path = REPO_ROOT / "scripts" / "bench" / "kpi_ledger.json"
RATINGS_DIR: Path = REPO_ROOT / "scripts" / "bench" / "ratings"
RATINGS_REL: str = "scripts/bench/ratings"
SLUG_MAX_CHARS: int = 80


@router.get("/kpi")
def get_kpi_ledger() -> dict[str, Any]:
    if not KPI_LEDGER_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "kpi_ledger_missing",
                "message": f"KPI ledger not found: {KPI_LEDGER_FILE}",
            },
        )
    try:
        ledger = json.loads(KPI_LEDGER_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "kpi_ledger_unparseable",
                "message": f"{KPI_LEDGER_FILE} is not valid JSON: {exc}",
            },
        ) from exc
    for key in ("kpis", "snapshots"):
        if key not in ledger:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "kpi_ledger_malformed",
                    "message": f"{KPI_LEDGER_FILE} missing '{key}'",
                },
            )
    return ledger


#----- ratings artifacts ------------------------------------------------------


class RatingEntry(BaseModel):
    """One clip as the rater page exports it.

    Shape matches the hand-exported artifact already in scripts/bench/ratings/;
    unrated clips carry nulls rather than being dropped, so the artifact always
    shows the full ladder.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    file: str = Field(min_length=1)
    inst_file: str | None = None
    params: str = ""
    # int | float, in that order: pydantic keeps an int an int, so the written
    # artifact matches the hand-exported one (machine_score 10, not 10.0).
    machine_score: int | float
    machine_rank: int | None = None
    si_sdr: float | None = None
    human_score: int | None = Field(default=None, ge=1, le=10)
    human_inst_score: int | None = Field(default=None, ge=1, le=10)
    note: str = ""


class RatingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str = Field(min_length=1)
    blind_mode: bool
    manifest: str | None = None
    ratings: list[RatingEntry] = Field(min_length=1)


class RatingsSaved(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    filename: str
    track: str
    saved_at: str
    rated_count: int
    total_count: int
    bytes: int


class RatingsFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    path: str
    track: str
    saved_at: str | None
    rated_count: int
    total_count: int
    bytes: int


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:SLUG_MAX_CHARS].strip("-")


def _stamp(now: datetime) -> str:
    """ISO 8601 basic format, UTC -- filename-safe (no colons)."""
    return now.strftime("%Y%m%dT%H%M%SZ")


@router.post("/ratings", response_model=RatingsSaved, status_code=201)
def save_ratings(payload: RatingsIn) -> RatingsSaved:
    scored = [r for r in payload.ratings if r.human_score is not None]
    if not scored:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ratings_empty",
                "message": (
                    "every human_score is null: nothing to save. Rate at least "
                    "one clip before saving."
                ),
            },
        )
    slug = _slug(payload.track)
    if not slug:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ratings_track_unslugifiable",
                "message": (
                    f"track {payload.track!r} has no alphanumeric characters, "
                    "so no filename can be derived from it"
                ),
            },
        )

    now = datetime.now(UTC)
    # _stamp() is second precision, so two saves of the same track within one
    # second need a disambiguating token or the second silently overwrites
    # the first. The token also makes the .part temp path below unique per
    # request, closing the same-name write race between concurrent saves.
    filename = f"ratings-{slug}-{_stamp(now)}-{secrets.token_hex(4)}.json"
    target = RATINGS_DIR / filename
    body = {
        "track": payload.track,
        "blind_mode": payload.blind_mode,
        "saved_at": now.isoformat().replace("+00:00", "Z"),
        "manifest": payload.manifest,
        "ratings": [r.model_dump() for r in payload.ratings],
    }
    text = json.dumps(body, indent=2) + "\n"

    RATINGS_DIR.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write leaves the .part behind, never a
    # half-written artifact that looks like a real rating set.
    part = target.with_suffix(".json.part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, target)

    return RatingsSaved(
        path=f"{RATINGS_REL}/{filename}",
        filename=filename,
        track=payload.track,
        saved_at=body["saved_at"],
        rated_count=len(scored),
        total_count=len(payload.ratings),
        bytes=len(text.encode("utf-8")),
    )


@router.get("/ratings", response_model=list[RatingsFile])
def list_ratings() -> list[RatingsFile]:
    if not RATINGS_DIR.exists():
        return []
    dated: list[tuple[str, RatingsFile]] = []
    for path in sorted(RATINGS_DIR.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "ratings_file_unparseable",
                    "message": f"{path} is not valid JSON: {exc}",
                },
            ) from exc
        entries = body.get("ratings")
        if not isinstance(entries, list):
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "ratings_file_malformed",
                    "message": f"{path} has no 'ratings' array",
                },
            )
        stat = path.stat()
        saved_at = body.get("saved_at")
        # Artifacts exported by hand before this endpoint existed carry no
        # saved_at; mtime is the only honest timestamp for them, and it is used
        # for ordering only -- saved_at stays null so nothing is invented.
        order_key = saved_at if isinstance(saved_at, str) else datetime.fromtimestamp(
            stat.st_mtime, tz=UTC).isoformat()
        dated.append((order_key, RatingsFile(
            filename=path.name,
            path=f"{RATINGS_REL}/{path.name}",
            track=str(body.get("track", "")),
            saved_at=saved_at if isinstance(saved_at, str) else None,
            rated_count=sum(1 for e in entries if e.get("human_score") is not None),
            total_count=len(entries),
            bytes=stat.st_size,
        )))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [model for _key, model in dated]


@router.get("/ratings/{filename}")
def get_ratings_file(filename: str) -> dict[str, Any]:
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ratings_filename_invalid",
                "message": f"{filename!r} is not a bare *.json filename",
            },
        )
    target = RATINGS_DIR / filename
    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ratings_file_missing",
                "message": f"no rating artifact at {RATINGS_REL}/{filename}",
            },
        )
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ratings_file_unparseable",
                "message": f"{target} is not valid JSON: {exc}",
            },
        ) from exc
