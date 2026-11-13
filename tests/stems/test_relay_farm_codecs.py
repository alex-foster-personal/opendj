"""The relay's farm side: backend wiring and the codec the farm encodes (STEM-76).

Split from test_relay_gates.py; the shared stand-ins live in relay_gates_support.py.

-Claude
"""

from __future__ import annotations

import copy
import functools
import io
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, create_model

from apps.stems.relay import contract as api
from apps.stems.relay.app import build_app
from apps.stems.relay.identity import (
    Caller,
    RateCap,
)
from tests.stems.relay_gates_support import (
    ALLOWLIST,
    CURRENT_CLIENT_CODECS,
    INVITED,
    _await_terminal,
    _client_fixture,  # noqa: F401  (the client fixture)
    _start,
    _stems_result,
    _StubVerifier,
)

# ----- round 12: the Modal backend is supplied by the entrypoint, never defaulted -----


def test_the_relay_refuses_to_assemble_without_a_backend() -> None:
    with pytest.raises(RuntimeError, match=r"scripts\.stems_relay_app"):
        build_app(allowlist=ALLOWLIST)


def test_the_entrypoint_imports_without_modal_and_names_the_backend() -> None:
    import importlib

    entrypoint = importlib.import_module("scripts.stems_relay_app")
    assert callable(entrypoint.build_relay_app)
    assert callable(entrypoint.modal_backend)


# ----- STEM-76: the relay carries the codec the farm actually encoded -----


@functools.cache
def _farm_encoded(codec: str) -> dict[str, Any]:
    """A separation result whose parts and audio block come from the farm's
    own encoder (``scripts.farm_stem_encode.encode_stems`` and
    ``audio_block``, the calls ``separate_track`` makes after demucs) run on
    real audio, not invented bytes (Codex P1 on #6821). Only the GPU half,
    demucs on Modal, is not run here: CI has no Modal credential, so that
    half stays open on STEM-75/76 until the redeployed relay serves a real
    MP3 separation."""
    # Plain imports: every caller is marked ``requires_audio_stack``, so a
    # host without numpy or soundfile reports UNAVAILABLE from conftest
    # before this runs, never a quiet skip (Codex P1 on #6821).
    import numpy as np

    from scripts import farm_stem_encode as fse

    rate, frames = 44100, 44100 * 2
    rng = np.random.default_rng(76)
    arrays = {part: (rng.standard_normal((frames, 2)) * 0.1).astype(np.float32) for part in api.STEM_PARTS}
    mix = sum(arrays.values()) / len(arrays)
    with tempfile.TemporaryDirectory() as tmp:
        source_bytes = len(fse.encode_mp3(mix, rate, Path(tmp) / "source.mp3", cbr_kbps=192))
    stems, size_policy = fse.encode_stems(
        arrays,
        rate,
        codec,
        source_bytes=source_bytes,
        source_kbps=fse.effective_bitrate_kbps(source_bytes, frames / rate),
    )
    result = _stems_result()
    result["stems"] = stems
    result["size_policy"] = size_policy
    result["audio"] = fse.audio_block(rate, 2, frames, codec)
    return result


def _relay_with(backend: Any) -> TestClient:
    return TestClient(
        build_app(
            verifier=_StubVerifier({"t": Caller(email=INVITED, subject="s")}),
            allowlist=ALLOWLIST,
            rate_cap=RateCap(limit=5, window=timedelta(hours=1)),
            backend=backend,
        )
    )


def _start_as_t(client: TestClient) -> str:
    created = client.post(
        api.PATH_SEPARATIONS,
        content=b"audio",
        headers={
            "Authorization": "Bearer t",
            api.HEADER_STABLE_ID: "a" * 40,
            api.HEADER_TIER: "M",
            api.HEADER_SOURCE_NAME: "a.mp3",
            api.HEADER_STEM_CODECS: CURRENT_CLIENT_CODECS,
        },
    ).json()
    return str(created["separation_id"])


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize(
    ("codec", "media_type", "ext", "sf_format"),
    [("mp3", "audio/mpeg", "mp3", "MP3"), ("flac", "audio/flac", "flac", "FLAC")],
)
def test_parts_are_served_and_saved_under_the_codec_the_farm_encoded(
    codec: str, media_type: str, ext: str, sf_format: str
) -> None:
    """The bundle must not name its bytes wrong.

    [if] MP3 parts are served or saved as FLAC [then] fail, [else stop].
    """
    import soundfile as sf

    from apps.stems.bundle_publish import stem_manifest
    from apps.stems.relay.client import separate_one

    result = _farm_encoded(codec)
    with _relay_with(lambda *_a, **_k: copy.deepcopy(result)) as client:
        out = separate_one(
            client,
            "",
            "t",
            audio_bytes=b"audio",
            stable_id="a" * 40,
            source_name="a.mp3",
            tier_key="M",
            deadline_s=30.0,
        )
        separation_id = _start_as_t(client)
        _await_terminal(client, separation_id, token="t")
        blob = client.get(
            api.PATH_SEPARATION_PART.format(separation_id=separation_id, part="vocals"),
            headers={"Authorization": "Bearer t"},
        )
    assert out["stem_ext"] == ext
    assert out["stem_codec"] == ext
    assert out["stems"]["vocals"] == result["stems"]["vocals"]
    # The farm's ladder audit survives the relay into the manifest (Codex on #6821).
    assert result["size_policy"], "the farm encoder produced no size policy to carry"
    assert out["size_policy"] == result["size_policy"]
    assert (
        stem_manifest("a" * 40, "/music/a.mp3", out, {"model": "htdemucs_ft"})["size_policy"] == result["size_policy"]
    )
    assert ("bit_depth" in out["audio"]) == (ext == "flac")
    assert blob.status_code == 200
    assert blob.headers["content-type"] == media_type
    # The bytes the relay served are what the media type claims: they decode.
    info = sf.info(io.BytesIO(blob.content))
    assert info.format == sf_format
    assert (info.samplerate, info.channels) == (44100, 2)


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
def test_a_codec_the_client_cannot_write_fails_the_separation() -> None:
    """[if] a codec the client cannot write succeeds [then] fail, [else stop].

    Real farm MP3 parts, labeled with a codec outside STEM_CODEC_FILES."""
    opus = copy.deepcopy(_farm_encoded("mp3"))
    opus["audio"]["codec"] = "opus"
    with _relay_with(lambda *_a, **_k: opus) as client:
        state = _await_terminal(client, _start_as_t(client), token="t")
    assert state["status"] == "failed"
    assert "opus" in state["error"]


# ----- STEM-76: a client from before STEM-76 still gets FLAC -----


def _relay_recording(asked: list[str], result: dict[str, Any] | None = None) -> TestClient:
    def backend(_audio: bytes, _sid: str, _name: str, _tier: str, stem_codec: str) -> dict[str, Any]:
        asked.append(stem_codec)
        return copy.deepcopy(result) if result is not None else _stems_result()

    return _relay_with(backend)


@pytest.mark.requirement("STEM-76")
@pytest.mark.parametrize(
    ("codecs", "source_name", "expected"),
    [
        (None, "a.mp3", "flac"),
        ("flac", "a.m4a", "flac"),
        (None, "a.ogg", "flac"),
        (CURRENT_CLIENT_CODECS, "a.mp3", "by-source"),
        ("mp3 , flac", "a.wav", "by-source"),
    ],
    ids=["legacy-mp3", "flac-only-m4a", "legacy-ogg", "current-mp3", "current-wav-spaced"],
)
def test_the_farm_is_asked_for_flac_unless_the_client_writes_every_codec(
    codecs: str | None, source_name: str, expected: str
) -> None:
    """[if] a client that names every part .flac is sent MP3 parts [then] fail, [else stop].

    Control: a current client still gets the tier's policy.

    An installed client from before STEM-76 sends no codecs header and saves
    whatever the relay returns as ``.flac``, so an MP3 answer was spent quota
    and a bundle refused at play time (Codex P1 on #6821). MUTATION TARGETS:
    route every client by the tier's policy (the legacy rows get
    ``by-source``), or send every client FLAC (the current rows get
    ``flac``).
    """
    asked: list[str] = []
    with _relay_recording(asked) as client:
        response = _start(client, "t", source_name=source_name, codecs=codecs)
        assert response.status_code == 202, response.text
        assert _await_terminal(client, response.json()["separation_id"], token="t")["status"] == "succeeded"
    assert asked == [expected]


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
def test_mp3_parts_for_a_flac_only_client_fail_the_separation() -> None:
    """[if] a backend answers a FLAC-only client with MP3 parts [then] the separation fails, [else stop].

    MUTATION TARGET: drop the relay's check of the returned codec against
    the one it asked for, and the legacy client is told it succeeded.
    """
    asked: list[str] = []
    with _relay_recording(asked, _farm_encoded("mp3")) as client:
        response = _start(client, "t", codecs=None)
        state = _await_terminal(client, response.json()["separation_id"], token="t")
    assert asked == ["flac"]
    assert state["status"] == "failed"
    assert "asked the backend for flac" in state["error"]


def test_the_shipped_client_lists_every_codec_it_writes() -> None:
    """[if] the client stops advertising MP3 [then] the relay would hand it FLAC forever, [else stop]."""
    import inspect

    from apps.stems.relay import client as relay_client

    assert 'api.HEADER_STEM_CODECS: ",".join(sorted(api.STEM_CODEC_FILES))' in inspect.getsource(
        relay_client.separate_one
    )


def _legacy_state_model() -> type[BaseModel]:
    """``SeparationState`` as a client from before STEM-76 has it: no ``size_policy``, extras forbidden."""
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in api.SeparationState.model_fields.items()
        if name != "size_policy"
    }
    return create_model("LegacySeparationState", __config__=ConfigDict(extra="forbid"), **fields)


@pytest.mark.requirement("STEM-76")
@pytest.mark.parametrize("codecs", [None, CURRENT_CLIENT_CODECS], ids=["legacy", "current"])
def test_a_poll_carries_size_policy_only_for_a_client_that_knows_it(client: TestClient, codecs: str | None) -> None:
    """[if] a client from before STEM-76 polls [then] its answer carries no ``size_policy`` key, [else stop].

    Control: a current client gets it.

    The old client parses every poll with ``extra="forbid"``, so even a null
    ``size_policy`` failed its poll after the quota was spent (Codex P1 on
    #6821). MUTATION TARGETS: always send the key (the legacy rows fail to
    parse), or never send it (the current rows lose the farm's audit).
    """
    response = _start(client, "tok-invited", codecs=codecs)
    assert response.status_code == 202, response.text
    separation_id = response.json()["separation_id"]
    queued = client.get(
        api.PATH_SEPARATION.format(separation_id=separation_id),
        headers={"Authorization": "Bearer tok-invited"},
    ).json()
    terminal = _await_terminal(client, separation_id)
    assert terminal["status"] == "succeeded"
    for body in (queued, terminal):
        if codecs is None:
            assert "size_policy" not in body
            _legacy_state_model().model_validate(body)
        else:
            assert "size_policy" in body
            api.SeparationState.model_validate(body)
