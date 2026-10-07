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
    audio_bytes: bytes, stable_id: str, source_name: str, tier_key: str
) -> dict[str, Any]:
    """Separate one track on Modal, with THIS process's credential."""
    import scripts.modal_vocal_farm as farm
    from apps.stems.tiers import get_tier

    preset = farm._resolve_preset(get_tier(tier_key).preset_tag, False)
    with farm.app.run():
        result = farm.separate_track.remote(
            audio_bytes,
            stable_id,
            source_name,
            preset.model,
            preset.overlap,
            preset.shifts,
            "local",  # return the stem bytes to this process
            source_name,
            preset.stamp(),
            "",
            farm.DEFAULT_STEM_CODEC,
        )
    if result.get("error"):
        raise RuntimeError(result["error"])
    result["preset"] = preset.stamp()
    result["model"] = {"name": preset.model, "version": farm.DEMUCS_VERSION}
    return result


def build_relay_app() -> FastAPI:
    """The relay with the Modal backend; every other gate resolves from the environment."""
    return build_app(backend=modal_backend)


__all__ = ["build_relay_app", "modal_backend"]
