"""Central separation-tier config: the S / M / L decisions, in one place.

THE ONE FILE TO EDIT when a separation choice changes. Everything downstream --
the Modal farm, the stems CLI, the webui estimate endpoint -- reads its knobs
from here rather than carrying its own copy.

A LADDER of separation products, nearest first:

  LOCAL  local      on this Mac. No network, no account. Works on a plane, and
                    makes the rest legible: everything above is the same work
                    moved off the laptop.
  S      quick      NOT APPLICABLE. Measured and found empty -- see below.
  M      optimal    the library default. What every track gets, and the thing
                    this whole iteration was aiming at.
  L      delicious  the control arm and the showcase. Slow, top of the quality
                    board, answers "how much are we leaving on the table".

WHY THE QUICK RUNG IS STILL HERE WITH NOTHING IN IT. It was measured: 5.8s
against optimal's 6.5s on a 4-minute track, so it would save 0.7 seconds. The
rung stays because an empty rung with its reason attached tells the next reader
that the gap WAS looked at; a deleted rung just invites someone to propose it
again. NOT_APPLICABLE is a result, not a gap in the work.

WHY THE GPU IS THE SAME FOR EVERY MODAL RUNG. Holding the GPU constant means a
tier comparison varies ONE thing (the separation config). Give S a cheaper card
and any S-vs-M number conflates model quality with hardware, which is the class
of confound that has already cost this project days. Cost is not a reason to
vary it: at Modal's per-second billing a whole track is cents on any card.

MINI-PRD
--------
Status key: `→` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran + works
as expected | `✔︎ ✅ 🎯` done + working + regression tests.

  ✔︎ ✅ 🎯 one ladder of rungs, each naming where it runs plus its model,
    overlap, shifts and GPU,
    with the EVIDENCE for the choice and the limits of that evidence recorded
    beside it rather than in a commit message.
    [if] a tier's preset tag is not in the farm's PRESETS [then ⛔️] raise at
         import, because the farm could not run it
    [if] a caller asks for a tier by key [then] it gets the same object the
         farm will actually run
    [if] two tiers accidentally share a preset tag [then ⛔️] raise -- a tier
         that is not distinct is not a tier

  ✔︎ ✅ 🎯 a per-track wall-clock estimate for any (tier, gpu) pair, derived
    from MEASURED throughput only.
    [if] a (tier, gpu) pair has never been benchmarked [then ⛔️] raise
         ThroughputNotMeasured naming the command that would measure it -- never
         interpolate, never fall back to a nearby card
    [if] a 4-minute track is estimated at tier M [then] the answer is
         fixed_overhead + 4 * seconds_per_audio_minute, both from a real run
    [if] a measurement is older than the model or overlap it claims to describe
         [then ⛔️] the provenance stamp makes that visible

  ✔︎ ✅ the estimate is reachable from CLI and HTTP, not just Python, so an
    agent can ask the same question a human can (AGENT-NATIVE PARITY).
    [if] `python -m apps.stems estimate --tier L --seconds 240` [then] prints
         the same number the API returns
    [if] the webui asks GET /api/v1/stems/estimate [then] it gets every tier at
         once, so a UI can show all three without three round trips

  → choosing the tier for a given track. That is policy and lives with the
    caller; this module only says what each tier costs and buys.
  → running separation. The farm does that.

-Claude
"""

from __future__ import annotations

from dataclasses import dataclass

# ----- GPU -------------------------------------------------------------------
# H100 is the FASTEST CARD THE PINNED STACK CAN USE, not the fastest card Modal
# sells. Modal offers B200 and B300 (Blackwell, sm_100), but the image pins
# torch 2.5.1 / cu124, which predates Blackwell support -- a B200 container
# would fail to launch a kernel, not merely run slowly.
#
# Moving to Blackwell is a real option and costs a torch bump to 2.7+/cu128.
# It is deliberately NOT bundled into this change because swapping the numerics
# underneath a measured quality ladder invalidates every listening result the
# tiers below are chosen from. Do it as its own measured migration: re-run
# scripts/bench/model_shootout and confirm the ladder still holds.
#
# H200 is the same Hopper compute with more memory. Demucs is not memory-bound
# on a 4-minute track, so it buys nothing here and costs 15% more per second.
DEFAULT_GPU: str = "H100"
SUPPORTED_GPUS: tuple[str, ...] = ("H100", "H200", "A100-80GB", "L40S", "L4")

# Modal's published per-second rates, https://modal.com/pricing, read Fri 24 Jul
# 2026. Used for reporting only; nothing schedules on price.
GPU_USD_PER_S: dict[str, float] = {
    "H100": 0.001097,
    "H200": 0.001261,
    "A100-80GB": 0.000694,
    "L40S": 0.000542,
    "L4": 0.000222,
}

# Starter plan allowance. Team plan is 50; raising this without raising the plan
# just queues containers.
MAX_CONCURRENT_GPUS: int = 10


# ----- tiers -----------------------------------------------------------------
@dataclass(frozen=True)
class Tier:
    """One rung of the separation ladder. Frozen: change the literal, not it.

    ``availability`` is part of the product, not an implementation note. A rung
    marked NOT_APPLICABLE stays on the ladder ON PURPOSE: it shows the shape of
    the space and records why that rung is empty, which a deleted rung cannot.
    Someone will ask "why is there no quick mode" again in three months.
    """

    key: str
    name: str
    where: str  # "local" | "modal"
    preset_tag: str
    model: str
    overlap: float
    shifts: int
    gpu: str  # "" for local
    purpose: str
    evidence: str
    evidence_strength: str  # MEASURED | PARTIAL | UNMEASURED
    availability: str = "AVAILABLE"  # AVAILABLE | NOT_APPLICABLE
    unavailable_because: str = ""
    # OUTPUT CODEC, and lossy is the default for bulk because of the INPUT, not
    # the output: sources here are already ~320 kbps MP3, so encoding the
    # separated stems losslessly preserves nothing that was ever in the file --
    # only the separation artifacts, exactly. Lossless is worth paying for on
    # anything that will be SCORED, since SI-SDR against a lossy stem measures
    # the codec as well as the model. It is not worth paying for on something a
    # DJ mutes and solos.
    codec: str = "opus"

    @property
    def stem_parts(self) -> tuple[str, str, str, str]:
        return ("vocals", "drums", "bass", "other")


# LADDER ORDER, slowest-and-nearest first. This is the order a UI should render.
TIER_ORDER: tuple[str, ...] = ("LOCAL", "S", "M", "L")

TIERS: dict[str, Tier] = {
    "LOCAL": Tier(
        key="LOCAL",
        name="local",
        where="local",
        preset_tag="htdemucs-local",
        model="htdemucs",
        overlap=0.25,
        shifts=0,
        gpu="",
        purpose=(
            "Separate on this Mac, no network and no Modal account. The rung "
            "that still works on a plane, and the one that makes the cost of "
            "the others legible: everything above it is the SAME WORK moved "
            "off the laptop."
        ),
        evidence=(
            "Local execution uses scripts/stem_bundle_worker.py and its pinned "
            "htdemucs worker in a separate PEP 723 environment. Private "
            "hardware timing captures are unavailable in this public copy. "
            "No per-minute estimate is published; estimate_seconds requires "
            "an actual measured throughput record and refuses when absent. "
            "The local worker and Modal optimal tier use different model "
            "families; aligning them requires separate verification."
        ),
        evidence_strength="PARTIAL",
    ),
    "S": Tier(
        key="S",
        name="quick",
        where="modal",
        preset_tag="hdemucs_mmi-ov0.1",
        model="hdemucs_mmi",
        overlap=0.1,
        shifts=0,
        gpu=DEFAULT_GPU,
        purpose=(
            "Live mashup use. Same model as M so the timbre matches what the "
            "library already holds; only the overlap is cut, so a track "
            "separated quick and the same track separated optimal do not sound "
            "like different processing chains."
        ),
        evidence=(
            "MEASURED, AND THE MEASUREMENT ARGUES AGAINST THIS TIER EXISTING. "
            "On H100, 3 repeats over 3 durations (tier_throughput.json): S is "
            "0.68 s/audio-minute against M's 0.75, so cutting overlap from 0.25 "
            "to 0.1 saves about 9% of GPU time -- which on a 4-minute track is "
            "5.8s versus 6.5s end to end. S saves SEVEN TENTHS OF A SECOND. "
            "That is not a different product, it is rounding. "
            "The quick tier was designed to solve a latency problem that the "
            "card already solved: at 6.5s a whole track, M is itself live-usable. "
            "RECOMMENDATION: collapse S into M and ship two tiers, unless a "
            "future card or a much longer track changes the arithmetic. Kept "
            "here because the three-tier surface was asked for, and because a "
            "tier removed silently is a decision nobody got to review. "
            "QUALITY at overlap 0.1 for hdemucs_mmi remains UNMEASURED: the "
            "overlap ladder was run on htdemucs, where rungs 3-6 sat inside one "
            "0.748 dB band. Assuming that transfers is an assumption, and now "
            "an assumption with no upside to buy."
        ),
        evidence_strength="MEASURED",
        availability="NOT_APPLICABLE",
        unavailable_because=(
            "Nothing to buy. Measured twice on independent samples, quick is "
            "not faster than optimal -- it came out nominally SLOWER both "
            "times, so the cheaper overlap buys no wall-clock at all. No "
            "figure is quoted here on purpose: call GET /stems/estimate, or "
            "read scripts/bench/tier_throughput.json, which is the only "
            "sanctioned source. It stays on the ladder to record that the gap "
            "was measured and found empty, rather than leaving someone to "
            "re-derive it in three months."
        ),
    ),
    "M": Tier(
        key="M",
        name="optimal",
        where="modal",
        preset_tag="hdemucs_mmi-ov0.25",
        model="hdemucs_mmi",
        overlap=0.25,
        shifts=0,
        gpu=DEFAULT_GPU,
        codec="opus",
        purpose=(
            "The library default. Every farmed track gets this unless someone "
            "asks otherwise."
        ),
        evidence=(
            "Public-dataset scoring is recorded in "
            "scripts/bench/MODEL-SHOOTOUT.md: 6 "
            "MUSDB18-HQ test tracks scored against true vocal stems: "
            "hdemucs_mmi has the best median SI-SDR of any test-set-clean "
            "model at 9.87 dB versus htdemucs at 9.52. "
            "Private listening ratings are unavailable. On outright wins, "
            "hdemucs_mmi took 2 of 6 and htdemucs_ft 4 of 6 AMONG CLEAN "
            "MODELS -- recomputed from model_shootout.json. Beware the '0 of "
            "6' figure in MODEL-SHOOTOUT.md line 16: that is the ALL-MODELS "
            "field, where the contaminated mdx_extra swept every track, and "
            "pairing it against a clean-field number for another arm compares "
            "two different denominators. Overlap 0.25 is used "
            "because it is the setting both the shootout and ladder_edge.json "
            "actually measured."
        ),
        evidence_strength="MEASURED",
    ),
    "L": Tier(
        key="L",
        name="delicious",
        where="modal",
        preset_tag="htdemucs_ft-ov0.25",
        model="htdemucs_ft",
        overlap=0.25,
        shifts=0,
        gpu=DEFAULT_GPU,
        # FLAC: this rung is the CONTROL every other rung is scored against.
        codec="flac",
        purpose=(
            "The control arm every other tier is judged against, and the "
            "showcase render for a track worth the wait. htdemucs_ft is four "
            "separate per-source fine-tuned models, so it costs roughly 4x the "
            "inference of a single model before overlap is counted."
        ),
        evidence=(
            "OVERLAP DROPPED 0.5 -> 0.25 Fri 24 Jul 2026, and the evidence was "
            "already in the repo. ladder_musdb.json scored htdemucs_ft on both "
            "tracks against TRUE MUSDB stems: Al James 6.984 at 0.25 versus "
            "7.093 at 0.5 (+0.109), Zeno 8.644 versus 8.592 (-0.052). Mean "
            "+0.029 dB and THE SIGN FLIPS, against this project's own 0.2 dB "
            "audibility bar. Overlap 0.5 was also the one config L used that "
            "none of its cited quality evidence was measured at -- the "
            "shootout and ladder_edge cfg-e both use overlap 0.25. "
            "The rung now matches the studies it quotes, for ~33% less "
            "compute. "
            "The public-dataset metrics are mixed: htdemucs_ft takes "
            "4 of 6 clean tracks outright. Its median "
            "SI-SDR is 9.52 dB against hdemucs_mmi's 9.87, and the paired "
            "per-track difference (M minus L) is mean +0.104 dB, sd 0.373, "
            "n=6 -- nominally in M's favour and not distinguishable from zero, "
            "let alone clearing this project's own 0.2 dB audibility bar. "
            "So L wins more tracks outright while losing on median and tying "
            "on the paired test. The defensible claim is the cost one: M gives "
            "up 0.07 dB of median for 4.8x less billed GPU time and 3.3x less "
            "wall clock, MEASURED ON H100 (tier_throughput.json). Do not "
            "quote the 6.3x figure from MODEL-SHOOTOUT.md: that is a GTX "
            "1660 slope ratio, and line 65 of that same file explicitly "
            "forbids transferring it across architectures. Whether L is audibly "
            "better is UNSETTLED and needs a blind M-vs-L A/B, which costs "
            "about 13 cents of H100 time on the existing rater harness. "
            "Baked into the farm image despite being four model files, "
            "because a rung that costs a cold weight download per container is "
            "a rung nobody ever runs."
        ),
        evidence_strength="PARTIAL",
    ),
}

DEFAULT_TIER: str = "M"


# ----- measured throughput ---------------------------------------------------
@dataclass(frozen=True)
class Throughput:
    """Measured cost of one tier on one card, fitted over several durations.

    TWO CLOCKS, because they answer different questions and confusing them is
    how this project got a 6.6x-wrong estimate once already:

      wall_*  what a caller WAITS. Includes container cold start, weight load,
              decode, separate, encode, and the return trip. This is the number
              a UI should show.
      gpu_*   what Modal BILLS: in-container seconds only. Cold start is not
              billed, so this is always the smaller of the two.

    Each is a straight-line fit ``fixed + minutes * slope`` over ``n_tracks``
    tracks spanning ``duration_span_s``. A fit from a single duration cannot
    separate fixed cost from slope, so the benchmark refuses to emit one.
    """

    tier_key: str
    gpu: str
    wall_fixed_s: float
    wall_s_per_audio_minute: float
    gpu_fixed_s: float
    gpu_s_per_audio_minute: float
    n_tracks: int
    duration_span_s: list[float]
    r_squared: float
    measured_by: str  # the exact command that produced it
    measured_at: str  # ISO date
    note: str = ""


class ThroughputNotMeasured(RuntimeError):
    """Raised instead of guessing. See the module mini-PRD."""


# Keyed "<tier>@<gpu>". EMPTY BY DESIGN until the benchmark runs -- an estimate
# invented from a spec sheet is exactly the hand-entered number this project has
# been burned by three times. Fill it ONLY from
# `scripts/bench/tier_throughput.py`, which writes this dict's contents to
# scripts/bench/tier_throughput.json for the estimator to read.
THROUGHPUT: dict[str, Throughput] = {}

_THROUGHPUT_JSON = "scripts/bench/tier_throughput.json"


def _key(tier_key: str, gpu: str) -> str:
    return f"{tier_key}@{gpu}"


def load_measured_throughput(repo_root) -> int:
    """Populate THROUGHPUT from the benchmark's JSON. Returns how many loaded."""
    import json
    from pathlib import Path

    path = Path(repo_root) / _THROUGHPUT_JSON
    if not path.exists():
        return 0
    raw = json.loads(path.read_text())
    for row in raw["measurements"]:
        t = Throughput(**row)
        THROUGHPUT[_key(t.tier_key, t.gpu)] = t
    return len(raw["measurements"])


def estimate_seconds(
    duration_s: float, tier_key: str = DEFAULT_TIER, gpu: str | None = None
) -> float:
    """Wall-clock seconds to separate one track of ``duration_s`` at ``tier``.

    Raises ThroughputNotMeasured rather than interpolating from a nearby card.
    """
    tier = get_tier(tier_key)
    card = gpu or tier.gpu
    tp = _require(tier.key, card)
    return tp.wall_fixed_s + (duration_s / 60.0) * tp.wall_s_per_audio_minute


def _require(tier_key: str, card: str) -> Throughput:
    tp = THROUGHPUT.get(_key(tier_key, card))
    if tp is None:
        raise ThroughputNotMeasured(
            f"no measured throughput for tier {tier_key} on {card}. "
            f"Measure it with: uv run --with modal python -m "
            f"scripts.bench.tier_throughput --tier {tier_key} --gpu {card}. "
            f"Measured pairs: {sorted(THROUGHPUT) or 'none'}"
        )
    return tp


def estimate_usd(
    duration_s: float, tier_key: str = DEFAULT_TIER, gpu: str | None = None
) -> float:
    """GPU cost of one track, from BILLED container seconds not wall clock.

    Using wall here would overcharge by the cold start, which Modal does not
    bill. Still a lower bound on the invoice: the scaledown window is real.
    """
    tier = get_tier(tier_key)
    card = gpu or tier.gpu
    if tier.where == "local":
        # Local execution has no marginal Modal bill. Zero marginal GPU cost is the
        # answer, and it is the number that makes the ladder legible: every rung
        # above this one is buying wall-clock, not quality.
        return 0.0
    if card not in GPU_USD_PER_S:
        raise KeyError(f"no published rate for {card}; known: {sorted(GPU_USD_PER_S)}")
    tp = _require(tier.key, card)
    billed_s = tp.gpu_fixed_s + (duration_s / 60.0) * tp.gpu_s_per_audio_minute
    return billed_s * GPU_USD_PER_S[card]


def estimate_batch_seconds(
    durations_s: list[float],
    tier_key: str = DEFAULT_TIER,
    gpu: str | None = None,
    max_concurrent: int = MAX_CONCURRENT_GPUS,
) -> float:
    """Wall clock for a whole batch, longest-first across ``max_concurrent`` slots.

    This is a GPU-SIDE estimate. It does NOT model the upload feeder, which has
    been the binding constraint on real runs from the Mac -- see
    apps/vocals/prefetch.py. Treat it as a floor, not a promise.
    """
    if not durations_s:
        return 0.0
    per_track = sorted(
        (estimate_seconds(d, tier_key, gpu) for d in durations_s), reverse=True
    )
    slots = [0.0] * max(1, min(max_concurrent, len(per_track)))
    for cost in per_track:  # longest-first onto the earliest-free slot
        slots[slots.index(min(slots))] += cost
    return max(slots)


def get_tier(key: str) -> Tier:
    k = key.upper()
    if k not in TIERS:
        raise KeyError(f"unknown tier {key!r}; known: {sorted(TIERS)}")
    return TIERS[k]


def modal_tiers() -> list[Tier]:
    """The rungs that run on Modal, in ladder order. LOCAL is not one."""
    return [TIERS[k] for k in TIER_ORDER if TIERS[k].where == "modal"]


def ladder() -> list[Tier]:
    """Every rung in render order, including NOT_APPLICABLE ones."""
    return [TIERS[k] for k in TIER_ORDER]


def _assert_tiers_coherent() -> None:
    """Fail at import if the tier table contradicts itself or the farm."""
    tags = [t.preset_tag for t in TIERS.values()]
    if len(set(tags)) != len(tags):
        raise RuntimeError(f"two tiers share a preset tag: {tags}")
    if set(TIER_ORDER) != set(TIERS):
        raise RuntimeError(
            f"TIER_ORDER {TIER_ORDER} does not cover TIERS {sorted(TIERS)}; a "
            "rung missing from the order would be invisible in every UI."
        )
    for t in TIERS.values():
        if t.where not in ("local", "modal"):
            raise RuntimeError(f"tier {t.key} has unknown where={t.where!r}")
        if t.where == "modal" and t.gpu not in SUPPORTED_GPUS:
            raise RuntimeError(f"tier {t.key} names unsupported gpu {t.gpu}")
        if t.where == "local" and t.gpu != "":
            raise RuntimeError(f"local tier {t.key} must not name a gpu")
        if t.availability not in ("AVAILABLE", "NOT_APPLICABLE"):
            raise RuntimeError(f"tier {t.key} bad availability {t.availability!r}")
        if t.availability == "NOT_APPLICABLE" and not t.unavailable_because:
            raise RuntimeError(
                f"tier {t.key} is NOT_APPLICABLE with no reason recorded. An "
                "empty rung with no explanation is worse than a deleted one."
            )
        if not t.preset_tag.startswith(t.model):
            raise RuntimeError(
                f"tier {t.key} preset tag {t.preset_tag!r} does not match "
                f"model {t.model!r}"
            )


_assert_tiers_coherent()
