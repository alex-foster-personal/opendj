"""Settings AI proposals name keys the client can apply (Codex P2 4130868880, PR #3896).

POST /api/v1/settings/ai-apply returns a proposal whose ``key`` the settings
overlay hands to ``applySettingChange``, which accepts only the ids in
``ALLOWED_SETTING_KEYS`` (apps/webui/frontend/src/lib/settings/apply.ts), the
settings catalog's ids. The server once proposed ``midi_enabled`` (the ui-prefs
disk field) while the catalog id is ``rb.midi_enabled``, so an accepted MIDI
proposal could never apply.

No fakes: the server allowlist is imported, the client allowlist is read from
its source, and the validator is a pure function.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.webui.server.routes import settings_ai

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "apps" / "webui" / "frontend" / "src" / "lib" / "settings"


def _client_keys() -> set[str]:
    src = (_FRONTEND / "apply.ts").read_text(encoding="utf-8")
    start = src.index("export const ALLOWED_SETTING_KEYS = [")
    block = src[start : src.index("] as const;", start)]
    keys = set(re.findall(r"^\t'([^']+)',?$", block, flags=re.MULTILINE))
    # Instrument check: a parse that finds nothing (or only a few keys) would
    # make every subset claim below vacuously true.
    assert {"theme", "rb.midi_enabled", "auto_sync.rekordbox"} <= keys, keys
    return keys


def _catalog_sources() -> list[str]:
    """catalog.ts plus the sibling modules it imports setting rows from.

    A row may live in its own file (midi-enabled-setting.ts) and be spliced into
    the catalog by value import, so the ids are read from every such module.
    """
    catalog = (_FRONTEND / "catalog.ts").read_text(encoding="utf-8")
    siblings = re.findall(r"^import \{[^}]+\} from '\./([\w-]+)';$", catalog, flags=re.MULTILINE)
    # Instrument check: an import parse that finds nothing would silently drop rows.
    assert "midi-enabled-setting" in siblings, siblings
    return [catalog, *((_FRONTEND / f"{name}.ts").read_text(encoding="utf-8") for name in siblings)]


def _catalog_ids() -> set[str]:
    src = "\n".join(_catalog_sources())
    ids = set(re.findall(r"\bid: '([^']+)'", src)) | set(re.findall(r"_todo\('([^']+)'", src))
    assert "rb.midi_enabled" in ids, "catalog parse found no MIDI setting"
    return ids


def test_every_server_proposal_key_is_one_the_client_applies() -> None:
    missing = set(settings_ai.ALLOWED_KEYS) - _client_keys()
    assert missing == set(), f"server proposes keys the client rejects: {sorted(missing)}"


def test_the_midi_proposal_key_is_the_catalog_id() -> None:
    assert "rb.midi_enabled" in _catalog_ids()
    assert "rb.midi_enabled" in settings_ai.ALLOWED_KEYS
    # Control: the disk field name is not a settings id, so it must not be proposed.
    assert "midi_enabled" not in _catalog_ids()
    assert "midi_enabled" not in settings_ai.ALLOWED_KEYS


@pytest.mark.parametrize(("raw", "expected"), [(True, True), ("off", False)])
def test_a_midi_proposal_validates_as_a_boolean(raw: object, expected: bool) -> None:
    assert settings_ai._validate_proposal("rb.midi_enabled", raw) is expected


def test_the_disk_field_name_is_refused() -> None:
    with pytest.raises(ValueError, match="disallowed"):
        settings_ai._validate_proposal("midi_enabled", True)
