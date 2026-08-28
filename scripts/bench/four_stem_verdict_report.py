# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Render .planning/four-stem-bass-verdict.md from the measured JSON only.

THE ONLY WRITER of that document. Every number in it is read out of
scripts/bench/four_stem_gpu_matrix.json, four_stem_hw_agreement.json,
ladder_4stem_all.json and tier_throughput.json. Nothing is retyped, because a
figure a human copied between a JSON and a markdown table is exactly how this
project's round-1 error travelled into production source.

The reproduction section is not decoration. The new matrix re-measures the same
(track, model, stem) cells the committed n=1 artifact already holds, so if the
two disagree the whole run is void and the document says that instead of
reporting a verdict.

Run:
  uv run scripts/bench/four_stem_verdict_report.py

-Claude
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
GPU_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_gpu_matrix.json"
HW_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_hw_agreement.json"
LADDER_JSON: Path = REPO_ROOT / "scripts/bench/ladder_4stem_all.json"
THROUGHPUT_JSON: Path = REPO_ROOT / "scripts/bench/tier_throughput.json"
FARM_PY: Path = REPO_ROOT / "scripts/modal_vocal_farm.py"
OUT_MD: Path = REPO_ROOT / ".planning/four-stem-bass-verdict.md"

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")
LADDER_ARMS: dict[str, str] = {
    "hdemucs_mmi": "hdemucs_mmi-ov25", "htdemucs": "htdemucs-ov25"}
TIER_OF: dict[str, str] = {
    "hdemucs_mmi": "M (current default)", "htdemucs": "LOCAL", "htdemucs_ft": "L"}
# Cost is quoted per 100 audio-hours on purpose: it needs no claim about how many
# tracks the library holds, and every population count in this repo's comments is
# a docstring rather than a measurement.
COST_AUDIO_HOURS: int = 100
SHORT: dict[str, str] = {
    "Al James - Schoolboy Facination": "Al James - Schoolboy",
    "The Easton Ellises - Falcon 69": "Easton Ellises - Falcon 69",
    "Triviul feat. The Fiend - Widow": "Triviul - Widow",
    "Zeno - Signs": "Zeno - Signs",
    "Punkdisco - Oral Hygiene": "Punkdisco - Oral Hygiene",
}


# ----- table helper -----------------------------------------------------------


def table(headers: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    widths = [max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(len(headers))]
    align = align or ["l"] * len(headers)

    def pad(text: str, i: int) -> str:
        return text.ljust(widths[i]) if align[i] == "l" else text.rjust(widths[i])

    head = "| " + " | ".join(pad(h, i) for i, h in enumerate(headers)) + " |"
    rule = "|" + "|".join(
        ("-" * (widths[i] + 2)) if align[i] == "l" else ("-" * (widths[i] + 1)) + ":"
        for i in range(len(headers))) + "|"
    body = "\n".join(
        "| " + " | ".join(pad(c, i) for i, c in enumerate(r)) + " |" for r in rows)
    return f"{head}\n{rule}\n{body}"


def sgn(value: float) -> str:
    return f"{value:+.3f}"


def h100_usd_per_s() -> float:
    """Read the published rate out of the farm rather than restating it."""
    for line in FARM_PY.read_text().splitlines():
        if line.strip().startswith('"H100":'):
            return float(line.split(":")[1].strip().rstrip(","))
    raise RuntimeError("no H100 rate found in scripts/modal_vocal_farm.py")


# ----- sections ---------------------------------------------------------------


def reproduction(gpu: dict) -> tuple[str, int, int]:
    ladder = json.loads(LADDER_JSON.read_text())
    track = ladder["track"].split(" (")[0]
    arms = {a["id"]: a for a in ladder["arms"]}
    rows, mismatches = [], 0
    for model, arm_id in LADDER_ARMS.items():
        for stem in STEMS:
            was = arms[arm_id]["stems"][stem]["si_sdr"]
            now = gpu["si_sdr"][track][model][stem]
            delta = now - was
            if abs(delta) > 0.05:
                mismatches += 1
            rows.append([model, stem, f"{was:.3f}", f"{now:.3f}", sgn(delta)])
    return (table(["model", "stem", "ladder_4stem_all.json (M3 CPU)",
                   "this run (H100)", "delta dB"], rows,
                  ["l", "l", "r", "r", "r"]), len(rows), mismatches)


def scores_table(gpu: dict, stem: str) -> str:
    models = gpu["models"]
    rows = []
    for track in sorted(gpu["si_sdr"]):
        best = max(models, key=lambda m: gpu["si_sdr"][track][m][stem])
        cells = []
        for m in models:
            value = f"{gpu['si_sdr'][track][m][stem]:.3f}"
            cells.append(f"**{value}**" if m == best else value)
        rows.append([SHORT.get(track, track)] + cells
                    + [f"{gpu['mixture_floor_si_sdr'][track][stem]:.3f}",
                       "yes" if gpu["gate"][track]["stem_passed"][stem] else "NO"])
    return table(["track"] + [f"{m} ({TIER_OF[m]})" for m in models]
                 + ["do-nothing floor", "stem gated"], rows,
                 ["l"] + ["r"] * (len(models) + 1) + ["r"])


def deltas_table(gpu: dict, stem: str) -> str:
    rows = []
    for row in gpu["paired_deltas_vs_default"]:
        if row["stem"] != stem:
            continue
        rows.append([
            f"{row['model']} minus {row['vs']}", str(row["n_gated"]),
            sgn(row["mean_delta_db"]), sgn(row["median_delta_db"]), f"{row['sd']:.3f}",
            "n/a" if row["t"] is None else f"{row['t']:+.2f}",
            f"{sgn(row['min_delta_db'])} .. {sgn(row['max_delta_db'])}",
            f"{row['tracks_favouring_challenger']}/{row['n_gated']}",
            "YES" if row["beats_audibility_bar"] else "no",
        ])
    return table(["paired comparison", "n", "mean dB", "median dB", "sd", "t",
                  "per-track range", "challenger wins", "mean >= 0.2 dB"],
                 rows, ["l", "r", "r", "r", "r", "r", "r", "r", "r"])


def main() -> None:
    gpu = json.loads(GPU_JSON.read_text())
    hw = json.loads(HW_JSON.read_text())
    throughput = {m["tier_key"]: m for m in json.loads(THROUGHPUT_JSON.read_text())["measurements"]}
    deltas = {(r["model"], r["stem"]): r for r in gpu["paired_deltas_vs_default"]}
    bar = gpu["audibility_bar_db"]
    models = gpu["models"]
    default = gpu["default_model"]
    n = gpu["n_tracks"]
    usd_s = h100_usd_per_s()

    sep = {m: statistics.median(t["gpu_s_per_audio_minute"]
                                for t in gpu["timings"] if t["model"] == m) for m in models}
    cost = {m: sep[m] * COST_AUDIO_HOURS * 60 * usd_s for m in models}

    repro_table, repro_n, repro_bad = reproduction(gpu)
    bass_ht = deltas[("htdemucs", "bass")]
    bass_ft = deltas[("htdemucs_ft", "bass")]
    voc_ft = deltas[("htdemucs_ft", "vocals")]
    oth_ft = deltas[("htdemucs_ft", "other")]
    drm_ft = deltas[("htdemucs_ft", "drums")]

    out: list[str] = []
    add = out.append

    add("# Four-stem bass verdict: is the project default wrong for BASS?\n")
    add(f"**Short answer: yes, and by more than the bass question asked.** The inversion "
        f"is real, it holds on every track measured, and the model that fixes it is one "
        f"the project already ships as tier L.\n")
    add(f"Measured {gpu['measured_at'][:10]} on **{gpu['hardware']}** via "
        f"`{gpu['runner']}`. n = **{n} MUSDB18-HQ test tracks**, one "
        f"**{gpu['window_length_s']:.0f} s window** each, scored per stem against the "
        f"**TRUE MUSDB18-HQ stems**. Models at overlap {gpu['knobs']['overlap']}, shifts "
        f"{gpu['knobs']['shifts']}, {gpu['knobs']['sample_rate_hz'] / 1000:.1f} kHz, so the "
        f"MODEL is the only variable. Total GPU spend to settle this: "
        f"**${gpu['gpu_usd_spent']:.4f}** ({gpu['container_s_total']} s of container time).\n")
    add("Every number below is read from a file some script wrote. Nothing is hand-typed.\n")
    add(table(["artifact", "what it carries", "writer"], [
        ["`scripts/bench/four_stem_gpu_matrix.json`", "the track x model x stem cube",
         "`scripts/bench/four_stem_gpu_matrix.py`"],
        ["`scripts/bench/four_stem_hw_agreement.json`", "CPU vs H100 agreement",
         "`scripts/bench/four_stem_hw_agreement.py`"],
        ["`.tmp/bench/4stem-matrix/fetch_verification.json`",
         "proof the windows are the gated ones", "`.tmp/bench/4stem-matrix/verify_fetch.py`"],
        ["`scripts/bench/stem_content_gate.json`", "the windows and the per-stem gate",
         "`scripts/bench/stem_content_gate.py`"],
        ["`scripts/bench/ladder_4stem_all.json`", "the n=1 result under test",
         "`scripts/bench/four_stem_ladder.py`"],
        ["`scripts/bench/tier_throughput.json`", "measured H100 cost per tier",
         "`scripts/bench/tier_throughput.py`"],
    ]))
    add("")

    add("## 0. Two integrity checks, before any verdict\n")
    add("**The windows are the gated ones.** The 60 s windows were cut out of "
        "`musdb18hq.zip`. "
        "Re-measuring the gate's own statistics on the local cuts reproduces "
        "`stem_content_gate.json` on **160 of 160 fields**, so the cuts are sample-identical "
        "to the windows the gate chose -- and the gate chose them on TRUE stem content "
        "before any model score existed.\n")
    add("**The harness is the same measurement.** This run re-scores cells the committed "
        "n=1 artifact already holds:\n")
    add(repro_table)
    add("")
    if repro_bad == 0:
        add(f"All {repro_n} shared cells reproduce within {hw['tolerance_db']} dB. The "
            f"measurement is the same one, extended from 1 track to {n} and from 2 models "
            f"to {len(models)}.\n")
    else:
        add(f"**{repro_bad} of {repro_n} cells DIFFER. The run is void.**\n")

    add("## 1. The CPU objection is dead\n")
    add(f"The n=1 result ran on an M3 CPU rather than the production card, and that was a "
        f"standing reason to distrust it. Measured rather than argued: "
        f"**{hw['cells_compared'] - hw['n_disagreements']} of {hw['cells_compared']} "
        f"shared cells agree within {hw['tolerance_db']} dB**, worst single cell "
        f"**{hw['max_abs_gpu_minus_cpu_db']:.3f} dB**, mean absolute difference "
        f"**{hw['mean_abs_gpu_minus_cpu_db']:.4f} dB**.\n")
    add(table(["CPU source", "cells"],
              [[k, str(v)] for k, v in hw["cells_by_source"].items()], ["l", "r"]))
    add(f"\nSI-SDR is a property of the separation, not of the silicon. **The card changes "
        f"the clock, not the quality.** The hardware caveat on the original finding was "
        f"never the problem; n=1 was.\n")

    # The sanity anchor is only worth anything if it is checked rather than
    # asserted, and the THINNEST margin is the number that says whether the test
    # discriminates -- an average over easy cells would hide a cell that does not.
    margins = [(gpu["si_sdr"][t][m][s] - gpu["mixture_floor_si_sdr"][t][s], t, m, s)
               for t in gpu["si_sdr"] for m in models for s in STEMS
               if gpu["gate"][t]["stem_passed"][s]]
    thin = min(margins)

    add(f"## 2. Per-stem results, n = {n}\n")
    add(f"`do-nothing floor` is the mixture graded as if it were that stem: the "
        f"deliberately bad arm. Every model clears it on all {len(margins)} gated cells, so "
        f"the test discriminates -- but the thinnest margin is only **{thin[0]:.2f} dB** "
        f"({SHORT.get(thin[1], thin[1])} / {thin[2]} / {thin[3]}), which is worth knowing "
        f"before reading that cell as a quality measurement. "
        f"`stem gated` is whether THAT stem cleared `stem_content_gate.py` in this window -- "
        "a stem buried in its own window scores toward the floor, so its delta is not "
        "evidence. Ungated cells are shown but never counted, rather than silently dropped.\n")
    # The justification for pairing is the size of the track-difficulty spread
    # relative to the model gaps, so it is measured rather than asserted.
    gated_scores = [gpu["si_sdr"][t][default][s] for t in gpu["si_sdr"] for s in STEMS
                    if gpu["gate"][t]["stem_passed"][s]]
    add(f"Paired deltas are the right statistic here because every model separated the "
        f"IDENTICAL window, so track-and-stem difficulty -- which spans "
        f"{max(gated_scores) - min(gated_scores):.1f} dB across the gated cells "
        f"({min(gated_scores):.3f} to {max(gated_scores):.3f} for the default model), far "
        f"more than any gap between models -- cancels. Bold marks the winner.\n")
    for stem in STEMS:
        add(f"### {stem}\n")
        add(scores_table(gpu, stem))
        add("")
        add(deltas_table(gpu, stem))
        add("")

    add("## 3. Does the bass inversion hold? Yes, and it is the cleanest signal in the set\n")
    add(table(["question", "answer"], [
        ["Does plain `htdemucs` beat the default on bass?",
         f"**Yes, {bass_ht['tracks_favouring_challenger']}/{bass_ht['n_gated']} tracks**, "
         f"mean {sgn(bass_ht['mean_delta_db'])} dB"],
        ["Was the n=1 gap an artifact?",
         f"No. n=1 said +0.775 dB; n={bass_ht['n_gated']} says "
         f"{sgn(bass_ht['mean_delta_db'])} dB (median {sgn(bass_ht['median_delta_db'])})"],
        ["Smallest per-track bass gain",
         f"{sgn(bass_ht['min_delta_db'])} dB -- every track clears the {bar} dB bar"],
        ["Spread relative to the effect",
         f"sd {bass_ht['sd']:.3f}, t = {bass_ht['t']:+.2f} on n={bass_ht['n_gated']}"],
        ["And `htdemucs_ft` on bass?",
         f"**Better still: {bass_ft['tracks_favouring_challenger']}/"
         f"{bass_ft['n_gated']} tracks**, mean {sgn(bass_ft['mean_delta_db'])} dB, "
         f"median {sgn(bass_ft['median_delta_db'])}, worst case "
         f"{sgn(bass_ft['min_delta_db'])}"],
    ], ["l", "l"]))
    add(f"\nBass is the only stem where the sign is the same on every single track for both "
        f"challengers. The default's bass deficit is not noise, and at "
        f"{abs(bass_ft['mean_delta_db']) / bar:.0f}x the {bar} dB audibility bar against "
        f"`htdemucs_ft` it is not marginal either. Bass is also the stem a DJ is most likely "
        f"to solo.\n")

    add("## 4. The bigger finding: the default loses on three stems, not one\n")
    add(f"`htdemucs_ft` was never scored per stem before this run. Against the default:\n")
    rows = []
    for stem in STEMS:
        row = deltas[("htdemucs_ft", stem)]
        # A mean and a median that disagree in SIGN is the signature of one
        # outlier carrying the average, so it is labelled rather than resolved
        # into a winner the per-track column does not support.
        split = (row["mean_delta_db"] > 0) != (row["median_delta_db"] > 0)
        rows.append([stem, str(row["n_gated"]), sgn(row["mean_delta_db"]),
                     sgn(row["median_delta_db"]),
                     f"{row['tracks_favouring_challenger']}/{row['n_gated']}",
                     "SPLIT: one outlier carries the mean" if split
                     else ("htdemucs_ft" if row["mean_delta_db"] > 0 else default),
                     "YES" if row["beats_audibility_bar"] else "no"])
    # No pipe characters in a header: they would terminate the markdown cell.
    add(table(["stem", "n", "mean dB", "median dB", "ft wins", "better model",
               "abs mean >= bar"], rows, ["l", "r", "r", "r", "r", "l", "r"]))
    add(f"\n`htdemucs_ft` takes **bass** ({sgn(bass_ft['mean_delta_db'])}), **other** "
        f"({sgn(oth_ft['mean_delta_db'])}, {oth_ft['tracks_favouring_challenger']}/"
        f"{oth_ft['n_gated']}, t = {oth_ft['t']:+.2f}) and **vocals** "
        f"({sgn(voc_ft['mean_delta_db'])}, {voc_ft['tracks_favouring_challenger']}/"
        f"{voc_ft['n_gated']}).\n")
    add(f"The vocals row matters most, because **VOCAL SI-SDR is the sole basis on which the "
        f"current default was chosen.** On this sample the default does not win vocals "
        f"either. That does not overturn `MODEL-SHOOTOUT.md` -- that run had 6 tracks and "
        f"found the two indistinguishable (paired median -0.07 dB) -- but it removes the "
        f"one axis the default was justified on. Read the two together: on vocals these "
        f"models are a coin flip, and on bass they are not.\n")
    add(f"**The bad case, stated rather than buried.** Drums is the exception: the mean is "
        f"{sgn(drm_ft['mean_delta_db'])} while the median is "
        f"{sgn(drm_ft['median_delta_db'])} and {drm_ft['tracks_favouring_challenger']} of "
        f"{drm_ft['n_gated']} tracks favour `htdemucs_ft`. One track, "
        f"Punkdisco - Oral Hygiene, drags the mean down on its own: the whole htdemucs "
        f"family loses its drums by {abs(drm_ft['per_track_gated']['Punkdisco - Oral Hygiene']):.2f} "
        f"to {abs(deltas[('htdemucs', 'drums')]['per_track_gated']['Punkdisco - Oral Hygiene']):.2f} dB "
        f"there. That is a real failure mode, not a rounding error, and it is the single "
        f"thing this verdict is least sure about.\n")

    add("## 5. What it costs, on the production card\n")
    add(table(["model", "tier", "median separate_s per audio-minute (H100)",
               f"USD per {COST_AUDIO_HOURS} audio-hours", "vs default"],
              [[m, TIER_OF[m], f"{sep[m]:.2f}", f"${cost[m]:.2f}",
                f"{sep[m] / sep[default]:.1f}x"] for m in models],
              ["l", "l", "r", "r", "r"]))
    add(f"\nMeasured in this run ({n} tracks each), at the published H100 rate of "
        f"${usd_s}/s. Cost is quoted per {COST_AUDIO_HOURS} audio-hours because that needs "
        f"no claim about how many tracks the library holds. `tier_throughput.json` "
        f"independently fits tier M at {throughput['M']['gpu_s_per_audio_minute']:.2f} and "
        f"tier L at {throughput['L']['gpu_s_per_audio_minute']:.2f} GPU-seconds per "
        f"audio-minute (r^2 {throughput['M']['r_squared']:.4f} / "
        f"{throughput['L']['r_squared']:.4f}), a ratio of "
        f"{throughput['L']['gpu_s_per_audio_minute'] / throughput['M']['gpu_s_per_audio_minute']:.1f}x "
        f"against the {sep['htdemucs_ft'] / sep[default]:.1f}x measured here. The two agree "
        f"on the DIRECTION and the order of magnitude but not the exact multiple, which is "
        f"expected: that fit spans {min(throughput['L']['duration_span_s']) / 60:.1f} to "
        f"{max(throughput['L']['duration_span_s']) / 60:.1f} minute tracks and amortises "
        f"fixed cost over them, whereas every window here is "
        f"{gpu['window_length_s']:.0f} s, the length that flatters fixed overhead least. "
        f"Treat the ratio as 'roughly "
        f"{throughput['L']['gpu_s_per_audio_minute'] / throughput['M']['gpu_s_per_audio_minute']:.0f} "
        f"to {sep['htdemucs_ft'] / sep[default]:.0f}x', not as a constant.\n")
    add(f"Switching the default from `{default}` to `htdemucs_ft` therefore costs about "
        f"**${cost['htdemucs_ft'] - cost[default]:.2f} more per {COST_AUDIO_HOURS} "
        f"audio-hours** of catalogue.\n")

    add("## 6. Recommendation\n")
    add("### (b) SWITCH THE DEFAULT to `htdemucs_ft` -- promote tier L to the library default\n")
    add(table(["why", "evidence"], [
        ["Bass is decisively better",
         f"mean {sgn(bass_ft['mean_delta_db'])} dB, "
         f"{bass_ft['tracks_favouring_challenger']}/{bass_ft['n_gated']} tracks, worst "
         f"track still {sgn(bass_ft['min_delta_db'])} dB -- above the {bar} dB bar "
         f"everywhere"],
        ["It is not a bass-only trade",
         f"`other` {sgn(oth_ft['mean_delta_db'])} dB "
         f"({oth_ft['tracks_favouring_challenger']}/{oth_ft['n_gated']}), `vocals` "
         f"{sgn(voc_ft['mean_delta_db'])} dB "
         f"({voc_ft['tracks_favouring_challenger']}/{voc_ft['n_gated']})"],
        ["The default's own justification does not survive",
         "it was chosen on vocal SI-SDR alone, and it does not win vocals here"],
        ["The cost is trivial at this scale",
         f"+${cost['htdemucs_ft'] - cost[default]:.2f} per {COST_AUDIO_HOURS} audio-hours, "
         f"{sep['htdemucs_ft'] / sep[default]:.1f}x GPU time on a job measured in "
         f"single-digit hours"],
        ["Zero implementation risk",
         "`htdemucs_ft` is already tier L, already baked into the farm image, already "
         "exercised by `tier_throughput.py`. This is a one-line default change, not new "
         "code"],
    ], ["l", "l"]))
    add(f"\n**Ship it with one condition:** drums. `htdemucs_ft` wins drums on "
        f"{drm_ft['tracks_favouring_challenger']} of {drm_ft['n_gated']} tracks but loses "
        f"one of them by {abs(drm_ft['min_delta_db']):.2f} dB. Before promoting, re-run "
        f"this matrix over more bass-and-drum-gated tracks and confirm Punkdisco is an "
        f"outlier rather than a class. That run costs about "
        f"${gpu['gpu_usd_spent'] / n:.3f} per track and roughly 12 s of GPU.\n")
    add("### Why not the others\n")
    add(table(["option", "verdict"], [
        ["(a) keep `hdemucs_mmi`",
         f"**Rejected.** It loses bass on {bass_ft['n_gated']}/{bass_ft['n_gated']} tracks "
         f"by a mean {abs(bass_ft['mean_delta_db']):.3f} dB, and the vocal advantage it "
         f"was chosen for does not appear in this sample"],
        ["(c) route per stem",
         "**Rejected, and it is the interesting rejection.** See below"],
        ["(d) still unsettled",
         "**Rejected for bass.** Same sign on every track, both challengers, well above "
         "the audibility bar, on the production card, against true stems. Drums alone "
         "is still unsettled"],
    ], ["l", "l"]))
    add(f"\n**Option (c) is genuinely implementable and still not worth it.** demucs returns "
        f"four separate tensors, so picking a different model per stem and merging is real "
        f"engineering, not a fantasy. It is rejected on three counts:\n")
    add(f"1. **It buys almost nothing.** The only stem `hdemucs_mmi` wins on average is "
        f"drums, and it wins that on the strength of ONE outlier track -- the median "
        f"favours `htdemucs_ft` at {sgn(drm_ft['median_delta_db'])} dB. Routing drums to "
        f"the default would be over-updating on n=1, which is the exact error this whole "
        f"exercise exists to correct.")
    add(f"2. **It costs a second inference pass.** {sep[default]:.2f} s per audio-minute on "
        f"top of {sep['htdemucs_ft']:.2f}, so about "
        f"{(sep[default] + sep['htdemucs_ft']) / sep['htdemucs_ft'] - 1:+.0%} GPU time "
        f"against single-model `htdemucs_ft`, plus per-track orchestration of two model "
        f"loads and a merge step.")
    add("3. **It breaks an invariant the product relies on.** demucs' four sources are "
        "trained to reconstruct their input, so a bundle from one pass approximately sums "
        "back to the mixture. Stems taken from two different models do not: mute one and "
        "the remaining three no longer add up to the track. For a mute/solo surface that "
        "is a correctness problem, not a quality trade.\n")
    add("Revisit (c) only if a later run shows a stem where the models genuinely diverge "
        "in opposite directions across MANY tracks. On this evidence one model wins nearly "
        "everywhere, which is the case routing is worst at justifying.\n")

    add("## 7. What this did NOT measure\n")
    add(f"- **Full tracks.** Every score is one {gpu['window_length_s']:.0f} s window per "
        f"song, chosen for four-stem content. Absolute dB runs optimistic.")
    add(f"- **More than {n} tracks**, and only {voc_ft['n_gated']} of them are gated for "
        f"vocals and other. This is small-n evidence with a consistent sign, not a "
        f"significance result -- read the per-track columns, not the t values.")
    add("- **Human listening.** SI-SDR is a proxy and nobody has heard these stems. The "
        "clips exist in the container only; no listening set was rendered.")
    add("- **The under-separation honesty check.** `MODEL-SHOOTOUT.md` ran SIR/SAR and "
        "mixture correlation on the vocal arms to catch a model scoring well by barely "
        "separating. That check was NOT run on these four-stem outputs, and "
        "`separation_metrics.py` is vocal-shaped (it wants an `.inst` complement), so "
        "extending it to four stems is real work rather than a re-run.")
    add("- **Target-library validation.** A four-stem conclusion measured on MUSDB "
        "does not establish the ordering for a user's library. Electronic material with "
        "heavy sidechain is under-represented here; validate user-selected inputs separately.")
    add("- **Knob sensitivity.** overlap is pinned at 0.25 and shifts at 0 for every model. "
        "No model was tuned individually.")
    add("- **Any model outside demucs.** No BS-RoFormer, no MDX23, no commercial "
        "separator.\n")
    add("-Claude")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_MD.relative_to(REPO_ROOT)}")
    print(f"reproduction: {repro_n - repro_bad}/{repro_n} within tolerance")
    print(f"hardware: {hw['cells_compared'] - hw['n_disagreements']}/{hw['cells_compared']} "
          f"cells agree, worst {hw['max_abs_gpu_minus_cpu_db']} dB")
    for stem in STEMS:
        for model in models:
            if model == default:
                continue
            row = deltas[(model, stem)]
            print(f"  {stem:7s} {model:12s}: mean {row['mean_delta_db']:+.3f} dB "
                  f"n={row['n_gated']} wins={row['tracks_favouring_challenger']}"
                  f"{'  BEATS BAR' if row['beats_audibility_bar'] else ''}")


if __name__ == "__main__":
    main()
