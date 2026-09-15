"""Fetch ASR lyric transcripts through the sync hub (LYRICS-07)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apps.cloud import asset_store
from apps.cloud.hub_lyrics_asr_client import fetch_lyrics_asr_presign
from apps.cloud.lyrics_asr_source import (
    LYRICS_ASR_HUB_UNREACHABLE,
    LYRICS_ASR_NOT_FOUND,
    LYRICS_ASR_PRESIGN_FAILED,
)
from apps.lyrics.asr_lines import group_asr_words_into_lines, parse_asr_words
from apps.lyrics.cache import Lyrics
from apps.shared.state.machine_identity import get_or_create_machine_id
from apps.sync_hub import config as sync_config
from apps.sync_hub import spoke_credential
from apps.sync_hub.transport import HttpTransport, HubTransport, SyncTransportError


class LyricsAsrFetchError(RuntimeError):
    """Hub or transport failure while fetching an ASR transcript."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LyricsAsrNotFoundError(RuntimeError):
    """The hub has no ASR transcript for this stable_id."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AsrTranscript:
    stable_id: str
    vocals_sha256: str | None
    language: str | None
    words: tuple[dict[str, Any], ...]


class AsrTransportFactory(Protocol):
    def build(self, data_dir: Path) -> HubTransport | None: ...


class HubAsrTransportFactory:
    def build(self, data_dir: Path) -> HubTransport | None:
        effective = sync_config.resolve_config(data_dir)
        if not effective.configured or effective.hub_url is None:
            return None
        machine_id = get_or_create_machine_id(data_dir)
        try:
            bearer = spoke_credential.read_credential(data_dir)
        except spoke_credential.SpokeCredentialError:
            return None
        return HttpTransport(effective.hub_url, bearer=bearer)


class AsrLyricsProvider:
    def __init__(
        self,
        data_dir: Path,
        *,
        transport_factory: AsrTransportFactory | None = None,
        transport: HubTransport | None = None,
        machine_id: str | None = None,
    ) -> None:
        self.data_dir = data_dir
        self._transport_factory = transport_factory or HubAsrTransportFactory()
        self._transport = transport
        self._machine_id = machine_id

    def fetch_transcript(self, stable_id: str) -> AsrTranscript:
        transport = self._transport or self._transport_factory.build(self.data_dir)
        if transport is None:
            raise LyricsAsrFetchError(
                LYRICS_ASR_HUB_UNREACHABLE,
                "CloudSync hub transport is not configured for ASR lyrics fetch",
            )
        machine_id = self._machine_id or get_or_create_machine_id(self.data_dir)
        try:
            presign = fetch_lyrics_asr_presign(
                transport,
                machine_id=machine_id,
                stable_id=stable_id,
            )
        except SyncTransportError as exc:
            raise _map_transport_error(exc) from exc
        try:
            body = asset_store.fetch_presigned_bytes(
                presign["url"],
                presign["content_hash"],
            )
        except asset_store.AssetStoreError as exc:
            raise LyricsAsrFetchError(
                LYRICS_ASR_HUB_UNREACHABLE,
                f"ASR transcript download failed: {exc}",
            ) from exc
        return _parse_transcript(body, stable_id)

    def fetch_lyrics(self, stable_id: str) -> Lyrics | None:
        """Return grouped line lyrics, or None when the transcript has zero words."""
        transcript = self.fetch_transcript(stable_id)
        words = parse_asr_words(list(transcript.words))
        if not words:
            return None
        lines = group_asr_words_into_lines(words)
        if not lines:
            return None
        return Lyrics(stable_id, "asr", lines)


def _map_transport_error(exc: SyncTransportError) -> Exception:
    if exc.code == LYRICS_ASR_NOT_FOUND:
        return LyricsAsrNotFoundError(
            LYRICS_ASR_NOT_FOUND,
            str(exc),
        )
    if exc.code == LYRICS_ASR_PRESIGN_FAILED:
        return LyricsAsrFetchError(LYRICS_ASR_PRESIGN_FAILED, str(exc))
    if exc.status_code is None:
        return LyricsAsrFetchError(LYRICS_ASR_HUB_UNREACHABLE, str(exc))
    return LyricsAsrFetchError(
        LYRICS_ASR_PRESIGN_FAILED,
        str(exc),
    )


def _parse_transcript(body: bytes, stable_id: str) -> AsrTranscript:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            f"ASR transcript JSON is invalid: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            "ASR transcript JSON must be an object",
        )
    if payload.get("schema_version") != 1:
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            "ASR transcript has unsupported schema_version",
        )
    payload_sid = payload.get("stable_id")
    if payload_sid != stable_id:
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            f"ASR transcript stable_id mismatch: expected {stable_id!r}, got {payload_sid!r}",
        )
    words = payload.get("words")
    if not isinstance(words, list):
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            "ASR transcript words must be a list",
        )
    vocals_sha256 = payload.get("vocals_sha256")
    if vocals_sha256 is not None and (
        not isinstance(vocals_sha256, str) or len(vocals_sha256) != 64
    ):
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            "ASR transcript vocals_sha256 is invalid",
        )
    language = payload.get("language")
    if language is not None and not isinstance(language, str):
        raise LyricsAsrFetchError(
            LYRICS_ASR_PRESIGN_FAILED,
            "ASR transcript language is invalid",
        )
    return AsrTranscript(
        stable_id=stable_id,
        vocals_sha256=vocals_sha256,
        language=language,
        words=tuple(word for word in words if isinstance(word, dict)),
    )


def language_to_iso3(language: str | None) -> str | None:
    if language is None or not language.strip():
        return None
    code = language.strip().casefold()
    if len(code) == 3:
        return code
    common = {
        "en": "eng",
        "es": "spa",
        "fr": "fra",
        "de": "deu",
        "it": "ita",
        "pt": "por",
    }
    return common.get(code, code)


__all__ = [
    "AsrLyricsProvider",
    "AsrTranscript",
    "AsrTransportFactory",
    "HubAsrTransportFactory",
    "LyricsAsrFetchError",
    "LyricsAsrNotFoundError",
    "language_to_iso3",
]
