"""STRUCTUI-12787: set-segment-display builds the same ui-prefs PUT as Settings."""

from __future__ import annotations

import json

import pytest

from apps.opendj_cli import EXIT_OK, api_cli


@pytest.mark.requirement("STRUCTUI-12787")
def test_set_segment_display_cli_put_matches_settings_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] CLI set-segment-display names a key [then] PUT body matches Settings id, [else stop]."""
    calls: list[list[str]] = []

    def _capture(request, **_kwargs):
        calls.append(list(request))
        return EXIT_OK

    monkeypatch.setattr(api_cli, "run", _capture)
    assert api_cli.run_set_segment_display(["sections_main_style", "m2_recolor"]) == EXIT_OK
    assert calls[0][0] == "PUT"
    assert calls[0][1] == "/api/v1/ui-prefs"
    assert json.loads(calls[0][3]) == {"sections_main_style": "m2_recolor"}


@pytest.mark.requirement("STRUCTUI-12787")
def test_set_waveform_overlay_sections_put_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] set_waveform_overlay sections [then] it targets show_sections on the bus, [else stop]."""
    from apps.opendj_cli.verbs import parse_invocation

    cmd = parse_invocation(["set_waveform_overlay", "sections", "true"]).command
    assert cmd == {"type": "set_waveform_overlay", "overlay": "sections", "enabled": True}
