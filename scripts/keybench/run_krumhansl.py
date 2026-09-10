"""Krumhansl-Kessler 1982 key candidate for the key-lane bench.

Runs under the REPO VENV (`python -m scripts.keybench.run_krumhansl`), not as a
PEP 723 script: it must import `apps.analysis_key.profiles` / `flags` / `canon`
rather than copy the profile vectors. chroma_cqt is the same call
`apps/analysis/backends/librosa.py` already makes (librosa defaults, 36 bins
per octave, auto tuning). Do not "upgrade" them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from apps.analysis_key import canon, flags, profiles
from scripts.keybench import _harness

PROFILES_PIN = "krumhansl-kessler-1982"
_PROFILES_PATH = "apps/analysis_key/profiles.py"


def _librosa():
    import librosa

    return librosa


def _profiles_git_sha() -> str:
    repo = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.check_output(
            ["git", "log", "-1", "--format=%h", "--", _PROFILES_PATH],
            cwd=repo,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha or "unknown"


def candidate_version() -> str:
    return f"{PROFILES_PIN}@{_profiles_git_sha()}+librosa-{_librosa().__version__}"


def result_from_estimate(estimate: profiles.KeyEstimate) -> dict[str, Any]:
    """Arm JSON for one estimate: both notations from the same `canon.Key`."""
    flag = flags.evaluate_tonal_center(estimate)
    return {
        "key_camelot": canon.to_camelot(estimate.key),
        "key_openkey": canon.to_open_key(estimate.key),
        "key_confidence": float(estimate.confidence),
        "no_tonal_center": bool(flag.no_tonal_center),
        "no_tonal_center_reason": flag.reason,
        "error": None,
    }


def analyze_wav(wav_path: str) -> dict[str, Any]:
    librosa = _librosa()
    y, sr = librosa.load(wav_path, sr=None, mono=True)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    return result_from_estimate(profiles.estimate_key_krumhansl(chroma))


def build_analyzer() -> _harness.Analyzer:
    return analyze_wav


def main(argv: list[str] | None = None) -> int:
    args = _harness.build_argparser(__doc__).parse_args(argv)
    return _harness.run(
        args,
        candidate="krumhansl",
        version=candidate_version(),
        device="cpu",
        build_analyzer=build_analyzer,
        extra_envelope={"librosa_version": _librosa().__version__},
    )


if __name__ == "__main__":
    sys.exit(main())
