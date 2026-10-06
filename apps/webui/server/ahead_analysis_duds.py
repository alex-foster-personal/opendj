"""Files the ahead drain cannot decode, recorded once and left alone (AHEAD-DUD-01).

A ``TrackUnreadable`` (ffmpeg cannot decode the file, e.g. exit 69) is a property
of THAT file, not a failure to retry and never a host verdict. It is kept per lane
with the file's size and mtime in ``state/ahead-analysis-duds.json``, so it
survives a restart. It is retried only by "Retry failed analysis" or when the
file changes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from apps.webui.server.coverage_outcomes import audio_token

FILENAME: str = "ahead-analysis-duds.json"


def is_dud(reason: str) -> bool:
    return reason.startswith("TrackUnreadable")


def _token(path: str | None) -> str | None:
    try:
        return None if path is None else audio_token(Path(path))
    except OSError:
        return None


class DudLedger:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._duds: dict[str, dict[str, list[str | None]]] = (
            json.loads(path.read_text()) if path is not None and path.exists() else {}
        )

    def record(self, lane: str, duds: Mapping[str, str], paths: Mapping[str, str]) -> None:
        self._duds.setdefault(lane, {}).update({sid: [_token(paths.get(sid)), why] for sid, why in duds.items()})
        self._save()

    def sync(self, lane: str, paths: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
        """(duds still current, ids whose file changed and so may be tried again)."""
        lane_duds = self._duds.get(lane, {})
        changed = [sid for sid, (token, _why) in lane_duds.items() if sid in paths and _token(paths[sid]) != token]
        for sid in changed:
            del lane_duds[sid]
        if changed:
            self._save()
        return {sid: str(why) for sid, (_token_, why) in lane_duds.items()}, changed

    def clear(self) -> None:
        self._duds = {}
        self._save()

    def _save(self) -> None:
        if self._path is not None:
            self._path.write_text(json.dumps(self._duds, sort_keys=True))
