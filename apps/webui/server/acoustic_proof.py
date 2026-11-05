"""AUDIO-DEVICE-04: fold the webview's acoustic proof into output-health.

The shell probe reads the HAL, and on Tue 3 Nov 2026 the HAL said a stalled
HYPERBOOM Bluetooth link was delivering. The acoustic proof is the check that
hears the room: the webview correlates the master bus with a wired or USB
microphone in 2 s windows while a deck plays, and posts its verdict here. A
fresh ``not_delivering`` proof overrides a shell ``ok``; it never upgrades a
shell fault to ``ok``, and a stale proof is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ACOUSTIC_PROOF_TTL_S = 10.0
# Mirrors the shell's ACOUSTIC_MIC_TRANSPORTS (speaker_link.rs). Bluetooth is
# never here: opening a Bluetooth input flips a headset into HFP.
ACOUSTIC_MIC_TRANSPORTS: frozenset[str] = frozenset({"usb", "builtin", "thunderbolt"})

AcousticVerdict = Literal["heard", "not_delivering", "inconclusive"]


@dataclass(frozen=True)
class AcousticProof:
    verdict: AcousticVerdict
    failed_windows: int
    windows_required: int
    peak_correlation: float | None
    mic_label: str
    mic_transport: str
    received_at: str
    received_monotonic_s: float

    def as_json(self, now_monotonic_s: float) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "failed_windows": self.failed_windows,
            "windows_required": self.windows_required,
            "peak_correlation": self.peak_correlation,
            "mic_label": self.mic_label,
            "mic_transport": self.mic_transport,
            "received_at": self.received_at,
            "age_s": round(now_monotonic_s - self.received_monotonic_s, 3),
        }


def overlay_acoustic_proof(
    shell: dict[str, Any], proof: AcousticProof | None, now_monotonic_s: float
) -> dict[str, Any]:
    """Return ``shell`` with ``acoustic_proof`` attached, and a fresh
    ``not_delivering`` proof turning an ``ok`` verdict into ``not_delivering``."""
    merged = dict(shell)
    fresh = proof is not None and now_monotonic_s - proof.received_monotonic_s <= ACOUSTIC_PROOF_TTL_S
    merged["acoustic_proof"] = proof.as_json(now_monotonic_s) if proof is not None and fresh else None
    if proof is None or not fresh or proof.verdict != "not_delivering":
        return merged
    if merged.get("verdict") == "ok":
        merged["verdict"] = "not_delivering"
        merged["device_delivering"] = False
        merged["reason"] = (
            f"Acoustic proof: {proof.mic_label} did not hear the master bus for "
            f"{proof.failed_windows} consecutive 2 s windows while a deck played"
        )
    return merged


__all__ = [
    "ACOUSTIC_MIC_TRANSPORTS",
    "ACOUSTIC_PROOF_TTL_S",
    "AcousticProof",
    "AcousticVerdict",
    "overlay_acoustic_proof",
]
