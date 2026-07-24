"""The model shootout's experiment spec: what we measure, on what, and why.

Stdlib only and deliberately free of torch, modal and numpy, so every consumer
can import it: the CUDA runner (scripts/bench/run_shootout_cuda.py), the Modal
runner (scripts/bench/run_shootout_cuda.py) and the report generator
(scripts/bench/shootout_report.py). One definition of the track list means a
result can never be attributed to a spread that a second copy quietly changed.

Fixing the sample set HERE, in code, before any scores exist, is the point. An
excerpt or a track chosen after seeing results is not evidence.

-Claude
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# All models run at the SAME knobs so the model is the only variable. overlap
# 0.25 is demucs' default, and the earlier ten-rung ladder showed the whole
# overlap 0.1 -> 0.5 sweep moves SI-SDR by under 0.2 dB, so sweeping it again
# would buy nothing that another track does not buy more of.
OVERLAP: float = 0.25
SHIFTS: int = 0
EXCERPT_LEN_S: float = 60.0

MODELS: tuple[str, ...] = (
    "mdx_q",
    "mdx",
    "mdx_extra_q",
    "mdx_extra",
    "htdemucs",
    "htdemucs_ft",
    "hdemucs_mmi",
)

# Training data per checkpoint, quoted from the demucs README's pre-trained model
# list (github.com/adefossez/demucs). The bool is `test_set_clean`, and it is the
# load-bearing field: demucs says outright that mdx_extra was "trained with extra
# training data (including MusDB test set)", and mdx_extra_q is its quantized
# twin. Scoring those two on MUSDB test tracks measures memorisation as much as
# separation, so the report must never rank them against the rest.
MODEL_PROVENANCE: dict[str, tuple[str, bool]] = {
    "mdx_q": ("MusDB HQ train only (quantized)", True),
    "mdx": ("MusDB HQ train only", True),
    "mdx_extra_q": ("MusDB train + extra INCLUDING MusDB test set (quantized)", False),
    "mdx_extra": ("MusDB train + extra INCLUDING MusDB test set", False),
    "htdemucs": ("MusDB train + 800 songs", True),
    "htdemucs_ft": ("MusDB train + 800 songs, per-source fine-tuned", True),
    "hdemucs_mmi": ("MusDB train + 800 songs", True),
}


@dataclass(frozen=True)
class Track:
    """One MUSDB18-HQ test track plus why it earned a slot in the spread."""

    title: str
    genre: str

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")


# Twelve of the fifty MUSDB18-HQ test tracks, chosen to spread the axes that
# plausibly change which separator wins: mix density, vocal type (sung, belted,
# screamed, rapped, processed), language, and production era. The first two are
# the earlier ladder's tracks, carried over so the new numbers can be checked
# against the old ones on identical material.
TRACKS: tuple[Track, ...] = (
    Track("Al James - Schoolboy Facination", "hip-hop, rapped male lead"),
    Track("Zeno - Signs", "pop rock, sung male lead"),
    Track("Timboz - Pony", "metal, screamed male over dense guitars"),
    Track("Sambasevam Shanmugam - Kaathaadi", "Tamil film, non-Western instrumentation"),
    Track("Enda Reilly - Cur An Long Ag Seol", "Irish-Gaelic folk, sparse acoustic"),
    Track("Cristina Vane - So Easy", "blues, female over slide guitar"),
    Track("Angels In Amplifiers - I'm Alright", "blues rock, belted female"),
    Track("Georgia Wonder - Siren", "produced pop, female"),
    Track("AM Contra - Heart Peripheral", "electronic dance, processed vocal"),
    Track("Punkdisco - Oral Hygiene", "electronic disco"),
    Track("Girls Under Glass - We Feel Alright", "industrial rock, German male"),
    Track("Signe Jakobsen - What Have You Done To Me", "soul pop, female"),
)


def provenance(name: str) -> tuple[str, bool]:
    """Training data and test-set cleanliness. Unknown models fail loudly, since
    ranking a model whose training data we cannot name implies a clean
    comparison the report could not support."""
    if name not in MODEL_PROVENANCE:
        raise RuntimeError(
            f"no training-data provenance recorded for model {name!r}; add it to "
            "MODEL_PROVENANCE before ranking it"
        )
    return MODEL_PROVENANCE[name]


def is_test_set_clean(name: str) -> bool:
    return provenance(name)[1]
