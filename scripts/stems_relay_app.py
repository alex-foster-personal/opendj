"""The stems relay as deployed: the relay package assembled with the Modal backend.

Supersedes: ``apps.stems.relay.app:build_app`` as the uvicorn launch target. That
factory now refuses to assemble without a backend and its error names this module.

Launch target::

    uv run --with modal uvicorn --factory scripts.stems_relay_app:build_relay_app

The Modal call lived inside ``apps.stems.relay.app`` as the backend's default until
round 12 of the SMARTEST-CI counter. It imports ``scripts.modal_vocal_farm``, and an
app package importing ``scripts`` was the edge that put ``stems`` (and through it
every scope ``scripts`` reaches) inside the 31-scope import cycle. The package now
takes the backend as an argument and refuses to assemble without one; this module
is where the real one is supplied. The import stays function-local because
``modal`` is not a repo dependency: the relay is deployed with ``uv run --with
modal``, and importing it at module scope would make this file unimportable
anywhere else, including in the test that checks it imports.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from apps.stems.relay.app import build_app


def modal_backend(
    audio_bytes: bytes,
    stable_id: str,
    source_name: str,
    tier_key: str,
    stem_codec: str | None = None,
) -> dict[str, Any]:
    """Separate one track on Modal, with THIS process's credential.

    Everything that can refuse the track runs before ``.remote()`` buys a GPU
    (STEM-75): the source extension against the tier's codec policy (before
    the farm, and so modal, is even imported), then the farm's mirror guard.
    """
    from apps.stems.bundle_publish import preset_for_tier

    # The test-pinned modal-free mirror of the farm's preset table, so every
    # refusal below happens before the farm, and so modal, is imported.
    preset = preset_for_tier(tier_key)
    args = relay_route_args(audio_bytes, stable_id, source_name, tier_key, preset, stem_codec)

    import scripts.modal_vocal_farm as farm

    farm.assert_route_contract()
    with farm.app.run():
        result = farm.separate_track.remote(*args)
    if result.get("error"):
        raise RuntimeError(result["error"])
    result["preset"] = preset.stamp()
    result["model"] = {"name": preset.model, "version": farm.DEMUCS_VERSION}
    return result


def relay_route_args(
    audio_bytes: bytes,
    stable_id: str,
    source_name: str,
    tier_key: str,
    preset: Any,
    stem_codec: str | None = None,
) -> tuple[Any, ...]:
    """The ``separate_track`` arguments for one relay separation.

    MP3 via the size ladder for an MP3/M4A source, FLAC for a lossless one or
    when the tier itself is FLAC (STEM-75). A source extension with no
    output-format policy raises ``UnknownSourceFormatError`` here, before
    anything is dispatched, rather than inside the GPU container.
    ``stem_codec`` is the relay's choice for its client
    (``stem_codec_for_client``: FLAC for one that writes only FLAC); None
    applies the tier's policy.
    """
    from apps.stems.bundle_publish import (
        route_stem_codec_for_source,
        separate_track_args,
    )

    return separate_track_args(
        audio_bytes=audio_bytes,
        stable_id=stable_id,
        name=source_name,
        source_path=source_name,
        preset=preset,
        stem_codec=stem_codec or route_stem_codec_for_source(tier_key, source_name),
    )


def build_relay_app() -> FastAPI:
    """The relay with the Modal backend; every other gate resolves from the environment."""
    return build_app(backend=modal_backend)


__all__ = ["build_relay_app", "modal_backend", "relay_route_args"]
