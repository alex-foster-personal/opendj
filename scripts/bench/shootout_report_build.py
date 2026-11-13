"""Assemble the full MODEL-SHOOTOUT markdown report from manifest data."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from shootout_report_constants import (
    _CLIPS_DIR,
    _MIN_TRACKS_FOR_RANKING,
    _RANK_GAP_FLAG,
    _is_clean,
)
from shootout_report_decision import (
    _anchor_verdict,
    _paired_deltas,
    _paired_table,
    _production_decision,
    _underpowered,
    _underpowered_banner,
    _withdrawn,
)
from shootout_report_model import ModelStats, _collect
from shootout_report_tables import (
    _cost_table,
    _divider,
    _honesty_table,
    _matrix_table,
    _ranking_table,
    _row,
)


@dataclass(frozen=True)
class _ReportFacts:
    manifest: dict[str, Any]
    stats: list[ModelStats]
    tracks: list[dict[str, Any]]
    models: list[str]
    ran: list[ModelStats]
    failed: list[ModelStats]
    clean: list[ModelStats]
    best_overall: ModelStats
    best_clean: ModelStats
    by_value: ModelStats
    honesty_lines: list[str]
    flagged: list[str]
    baseline_median: float
    failed_anchor: list[str]
    config: dict[str, Any]
    gated: bool


def _collect_report_facts(manifest: dict[str, Any]) -> _ReportFacts:
    stats, tracks = _collect(manifest)
    models = manifest["models"]
    ran = [s for s in stats if s.ran]
    failed = [s for s in stats if not s.ran]
    ranked = sorted(ran, key=lambda s: s.median_si_sdr, reverse=True)
    clean = [s for s in ranked if _is_clean(s.name)]
    best_overall = ranked[0]
    best_clean = clean[0]
    by_value = sorted(clean, key=lambda s: s.db_per_second, reverse=True)[0]
    honesty_lines, flagged = _honesty_table(stats)
    baseline_median = statistics.median([t["baseline_mixture"]["si_sdr"] for t in tracks])
    # Sanity anchor: the do-nothing mixture is the deliberately bad arm. A test
    # that cannot separate it from the real models is broken, so say so loudly.
    failed_anchor = [s.name for s in ran if s.median_si_sdr <= baseline_median]
    config = manifest["config"]
    gated = _underpowered(len(tracks))
    return _ReportFacts(
        manifest=manifest,
        stats=stats,
        tracks=tracks,
        models=models,
        ran=ran,
        failed=failed,
        clean=clean,
        best_overall=best_overall,
        best_clean=best_clean,
        by_value=by_value,
        honesty_lines=honesty_lines,
        flagged=flagged,
        baseline_median=baseline_median,
        failed_anchor=failed_anchor,
        config=config,
        gated=gated,
    )


def _header_section(facts: _ReportFacts) -> list[str]:
    tracks = facts.tracks
    models = facts.models
    config = facts.config
    return [
        "# Vocal separation model shootout",
        "",
        f"{len(tracks)} MUSDB18-HQ test "
        f"{'track' if len(tracks) == 1 else 'tracks'}, {len(models)} models, "
        f"{facts.manifest['excerpt_len_s']:.0f}s vocal-dense excerpt per track, scored "
        "against the TRUE vocal stems.",
        f"All models run at identical knobs (overlap {config['overlap']}, shifts "
        f"{config['shifts']}) on a {config['gpu']}, so the model is the only variable.",
        "",
        "Primary metric is SI-SDR in dB against ground truth, higher is better. The "
        "do-nothing baseline (the mixture graded as if it were a vocal estimate) has a "
        f"median of {facts.baseline_median:.2f} dB and is the deliberately bad arm: a test "
        "that cannot tell it from a real separator is broken.",
        "",
        *_underpowered_banner(len(tracks)),
        "## Read this before the tables: two of these models saw the test set",
        "",
        'The demucs README states that `mdx_extra` was "trained with extra training '
        'data (**including MusDB test set**)", and `mdx_extra_q` is its quantized '
        "twin. Every track below is a MUSDB18-HQ **test** track, so those two models "
        "are being graded on material they trained on. Their scores measure "
        "memorisation as well as separation and CANNOT be compared with the others.",
        "",
        "`mdx` is the same architecture trained on the train split only, so the "
        "`mdx` versus `mdx_extra` gap is the cleanest available read on how much the "
        "leak is worth. The headline below is therefore taken from the uncontaminated "
        "models only; the leaked pair is reported as evidence, not as a recommendation.",
        "",
        "## Headline",
        "",
    ]


def _headline_section(facts: _ReportFacts) -> list[str]:
    if facts.gated:
        return [
            f"Withheld. {len(facts.tracks)} "
            f"{'track is' if len(facts.tracks) == 1 else 'tracks are'} not enough to name a "
            "best model, and a headline is exactly the form in which an underpowered "
            f"number escapes into a decision. Re-run to at least "
            f"{_MIN_TRACKS_FOR_RANKING} tracks.",
            "",
        ]
    config = facts.config
    flagged = facts.flagged
    failed_anchor = facts.failed_anchor
    best_clean = facts.best_clean
    by_value = facts.by_value
    best_overall = facts.best_overall
    return [
        f"- Best median quality among test-set-clean models: **{best_clean.name}** "
        f"at {best_clean.median_si_sdr:.2f} dB (min {min(best_clean.si_sdr):.2f}, "
        f"max {max(best_clean.si_sdr):.2f}), winning {best_clean.wins} of "
        f"{len(facts.tracks)} tracks outright.",
        f"- Best quality per second among clean models: **{by_value.name}** at "
        f"{by_value.db_per_second:.2f} dB/s ({by_value.median_si_sdr:.2f} dB in "
        f"{by_value.median_separate_s:.2f}s median) on {config['gpu']}, a figure "
        "that must be re-measured on the production GPU before it is used.",
        f"- Top of the raw board is **{best_overall.name}** at "
        f"{best_overall.median_si_sdr:.2f} dB"
        + (
            ", which is one of the leaked models. Treat that as a measurement of "
            "train-on-test contamination, not of quality."
            if not _is_clean(best_overall.name)
            else ", which is test-set clean."
        ),
        "- Under-separation flags: "
        + (
            f"**{', '.join(flagged)}** (see the honesty check below)."
            if flagged
            else "none. No model's SI-SDR advantage traces to leaving the vocal buried."
        ),
        "- Sanity anchor: "
        + (
            f"**{', '.join(failed_anchor)}** failed to beat the do-nothing mixture. "
            "Investigate before trusting anything else here."
            if failed_anchor
            else "every model beat the do-nothing mixture baseline, so the test discriminates."
        ),
        "",
    ]


def _matrix_section(facts: _ReportFacts) -> list[str]:
    out: list[str] = [
        "## SI-SDR by track and model (dB vs true vocals)",
        "",
    ]
    out += _matrix_table(facts.tracks, facts.models, summarise=not facts.gated)
    out += [
        "",
        "The `mix baseline` column is that track's SI-SDR if you skipped separation "
        "entirely. Tracks where it is high are ones where the vocal already dominates "
        "the mix, so every model looks good; tracks where it is low are the real test.",
        "",
    ]
    return out


def _ranking_section(facts: _ReportFacts) -> list[str]:
    out: list[str] = [
        "## Ranking by median SI-SDR, with spread",
        "",
    ]
    if facts.gated:
        out += _withdrawn(len(facts.tracks), "a ranking and a spread")
    else:
        out += _ranking_table(facts.stats)
        out += [
            "",
            "Spread is max minus min across tracks. A large spread means the model is "
            "material-dependent and a single-track measurement of it proves nothing.",
        ]
    return out


def _cost_section(facts: _ReportFacts) -> list[str]:
    config = facts.config
    return [
        *_cost_table(facts.stats),
        "",
        f"All timings come from a **{config['gpu']}**. `separate_s` is GPU inference "
        "time only and excludes model loading, which production pays once. It is "
        "reported as a MEDIAN across tracks on purpose: the first transformer model to "
        "run in a fresh process absorbs one-off cuDNN kernel selection, which inflated "
        f"a single measurement by roughly 4x on track one. A median over {len(facts.tracks)} "
        "tracks is immune to that; a mean would not be.",
        "",
        "**These ratios do not transfer across hardware.** `htdemucs` and `htdemucs_ft` "
        "are hybrid transformers while `hdemucs_mmi` and the mdx family are pure "
        "convolutional, and those two shapes scale differently on different GPUs: "
        "transformer attention gains far more from modern tensor cores than stacked "
        "convolutions do. A cost ratio measured on the Turing-generation card above can "
        "compress, or invert, on the Ada-generation L4 that production would use. Treat "
        "the dB-per-second column as evidence that a large gap exists on THIS hardware, "
        "and re-measure the shortlisted models on the production GPU before any cost "
        "figure is used to justify a model choice.",
        "",
    ]


def _honesty_section(facts: _ReportFacts) -> list[str]:
    flagged = facts.flagged
    return [
        "## Honesty check: is any SI-SDR win bought by under-separating?",
        "",
        "SI-SDR does not punish leftover accompaniment as harshly as it punishes "
        "distortion, so a cautious model can score well by barely separating. SIR "
        "(signal to interference ratio, BSS Eval v4 with both true sources supplied) "
        "measures exactly the accompaniment that leaked through, and correlation with "
        "the mixture catches an estimate that is mostly just the input again. A model "
        "is flagged when it ranks at least "
        f"{_RANK_GAP_FLAG} places better on SI-SDR than on SIR AND sits above the "
        "cross-model median mixture correlation.",
        "",
        *facts.honesty_lines,
        "",
        (
            "No model is flagged. The SI-SDR ordering is not an artifact of under-separation."
            if not flagged
            else "Flagged models should not be adopted on SI-SDR alone. Read the flag "
            "together with the SAR column before concluding anything, because two very "
            "different models trip this rule. A flagged model with a LOW SAR is the bad "
            "case: it is leaving accompaniment in and adding artifacts, and its SI-SDR "
            "is hollow. A flagged model with a HIGH SAR is the interesting case: it "
            "separates less aggressively but what it returns is cleaner, which is a "
            "real trade (less bleed-through removal, fewer swirly artifacts) and often "
            "the more usable stem in a DJ context. Either way, audition it."
        ),
        "",
    ]


def _perceptual_proxy_section(facts: _ReportFacts) -> list[str]:
    lsd_ranked = sorted(facts.ran, key=lambda s: s.median_lsd)
    out: list[str] = [
        "## Perceptual proxy: log-spectral distance (lower is better)",
        "",
        _row(["model", "median LSD dB", "median SAR dB", "median SIR dB"]),
        _divider(4),
    ]
    out.extend(
        _row(
            [
                s.name,
                f"{s.median_lsd:.2f}",
                f"{s.median_sar:.2f}",
                f"{s.median_sir:.2f}",
            ]
        )
        for s in lsd_ranked
    )
    out += [
        "",
        "LSD is scale-aligned before comparison. It agrees with SI-SDR only loosely, "
        "which is expected: LSD is blind to phase and weights quiet bins equally, so "
        "treat it as a second opinion rather than a tie-breaker.",
        "",
    ]
    return out


def _leak_sizing_section(facts: _ReportFacts) -> list[str]:
    tracks = facts.tracks
    out: list[str] = [
        "## Sizing the train-on-test leak, and the quantization cost",
        "",
        "Same architecture, different training data or precision, so each pair isolates one variable.",
        "",
        _row(
            [
                "pair",
                "what it isolates",
                "median paired delta",
                "range",
                "tracks favouring the right-hand model",
            ]
        ),
        _divider(5, left=2),
    ]
    by_name = {s.name: s for s in facts.ran}
    for left, right, isolates in (
        ("mdx", "mdx_extra", "MusDB test set in the training data"),
        ("mdx_q", "mdx_extra_q", "MusDB test set in the training data (quantized)"),
        ("mdx", "mdx_q", "8-bit quantization"),
        ("mdx_extra", "mdx_extra_q", "8-bit quantization"),
        ("htdemucs", "htdemucs_ft", "per-source fine-tuning at roughly 4x compute"),
    ):
        if left not in by_name or right not in by_name:
            continue
        deltas = _paired_deltas(tracks, right, left)
        if not deltas:
            continue
        wins = sum(1 for d in deltas if d > 0)
        out.append(
            _row(
                [
                    f"`{left}` vs `{right}`",
                    isolates,
                    f"**{statistics.median(deltas):+.2f} dB**",
                    f"{min(deltas):+.2f} to {max(deltas):+.2f}",
                    f"{wins}/{len(deltas)}",
                ]
            )
        )
    out += [
        "",
        "The first two rows are the leak estimate, and the win column is the part that "
        "matters: the contaminated checkpoint does not merely average higher, it wins "
        "on every single track. A genuinely better architecture would trade wins across "
        "material of this diversity (metal, Tamil film, Irish folk, blues). A model "
        "that never loses on the exact songs it trained on is showing recall, not "
        "skill. On unseen material that advantage should shrink or vanish.",
        "",
        "The quantization rows are the incidental finding: 8-bit costs 0.2 dB or less, "
        "so quantized checkpoints are essentially free quality-wise and the smaller "
        "download is worth having.",
        "",
    ]
    return out


def build_report(manifest: dict[str, Any]) -> str:
    facts = _collect_report_facts(manifest)
    out: list[str] = []
    out += _header_section(facts)
    out += _headline_section(facts)
    out += _matrix_section(facts)
    out += _ranking_section(facts)
    out += ["", "## Quality per unit compute", ""]
    if facts.gated:
        out += _withdrawn(len(facts.tracks), "a cost ranking")
        out += [""]
        return "\n".join(out + _tail_sections(facts.manifest, facts.tracks, facts.config, facts.failed))
    out += _cost_section(facts)
    out += _honesty_section(facts)
    out += _perceptual_proxy_section(facts)
    out += _production_decision(facts.ran, facts.tracks)
    out += _leak_sizing_section(facts)
    out += _tail_sections(facts.manifest, facts.tracks, facts.config, facts.failed)
    return "\n".join(out)


def _tail_sections(
    manifest: dict[str, Any],
    tracks: list[dict[str, Any]],
    config: dict[str, Any],
    failed: list[ModelStats],
) -> list[str]:
    """Sections that stay valid at any sample size.

    Everything here is either a per-track measurement or a statement about
    method, so none of it depends on having enough tracks to rank across. That
    is why the underpowered path can jump straight here.
    """
    out: list[str] = []
    out += _anchor_verdict(tracks)
    out += [
        "## What this run did not measure",
        "",
        "- Full tracks. Every score is one "
        f"{manifest['excerpt_len_s']:.0f}s vocal-dense excerpt per song, chosen for "
        "maximum vocal energy, so quiet intros and instrumental passages are excluded "
        "and absolute dB values run optimistic.",
        "- Human listening. SI-SDR, SIR, SAR and LSD are all proxies, and no one has "
        "listened to these stems yet."
        + (
            " The clips in `clips-shootout/` are there to make that check cheap."
            if _CLIPS_DIR.is_dir()
            else " Listening clips have NOT been generated for this run; run `shootout_clips.py` to produce them."
        ),
        "- Knob sensitivity. overlap is pinned at "
        f"{config['overlap']} and shifts at {config['shifts']} for every model. The "
        "earlier ladder found under 0.2 dB across the whole overlap range, which is why "
        "they are pinned, but no model was tuned individually.",
        "- Real DJ library material. MUSDB18-HQ is mostly live-band recordings. "
        "Electronic material with heavy sidechain and reverb-drenched vocals is "
        "under-represented relative to the actual catalogue.",
        "- Any model outside demucs. No comparison against MDX23, BS-RoFormer, or the commercial separators.",
        f"- Six of the twelve planned tracks. The fixed sample set in "
        f"`shootout_spec.py` is 12 tracks; the run was stopped after {len(tracks)} once "
        "the product owner judged existing stem quality already sufficient. Hip-hop, "
        "pop rock, metal, Tamil film, Irish folk and blues are represented; produced "
        "pop, electronic, industrial rock and soul are not. The six that ran were the "
        "first six of a list fixed before any score existed, so they are a truncation "
        "rather than a selected subset.",
        "",
    ]
    if failed:
        out += [
            "## Models that failed",
            "",
        ]
        for s in failed:
            out.append(f"- `{s.name}`: failed on all {len(s.failures)} tracks.")
        out.append("")
    out += [
        "## Per-track windows",
        "",
        _row(["track", "genre", "window", "vocal rms dBFS", "silent hops"]),
        _divider(5, left=2),
    ]
    for track in tracks:
        window = track["window"]
        out.append(
            _row(
                [
                    track["track"],
                    track["genre"],
                    f"{window['start_s']:.1f}s to {window['end_s']:.1f}s",
                    f"{window['rms_dbfs']:.1f}",
                    f"{100 * window['silent_frac']:.1f}%",
                ]
            )
        )
    out += [
        "",
        "Windows are chosen on TRUE vocal energy, never on a separator's guess, and the "
        "identical window is cut from the mixture and the truth so the pair is "
        "sample-aligned.",
        "",
        "Generated by `scripts/bench/shootout_report.py` from `model_shootout.json`.",
        "",
        "-Claude",
        "",
    ]
    return out
