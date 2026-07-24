"""Q0 run for free: recompute the raw vocals/mix ratio envelope from the 21
existing stem bundles that overlap the vocal cache. No GPU, no Modal, no spend gate.

Tests strategist's F3 mechanism (b): does the ratio blow up when mix energy is low,
fabricating regions during instrumental / quiet passages?
"""

import glob
import json
import os
import sqlite3
import wave

import numpy as np

HOP_S = 0.5
ON_RATIO = 0.10
OFF_RATIO = 0.05
MERGE_GAP_S = 1.5
MIN_REGION_S = 1.0
STEMS = ("vocals", "drums", "bass", "other")


def read_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, nch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dt = {2: np.int16, 4: np.int32}.get(sw)
    if dt is None:
        raise RuntimeError(f"unhandled sample width {sw} in {path}")
    a = np.frombuffer(raw, dtype=dt).astype(np.float32)
    a /= float(np.iinfo(dt).max)
    return a.reshape(-1, nch).mean(axis=1), sr


def rms_env(mono: np.ndarray, sr: int) -> np.ndarray:
    hop = int(sr * HOP_S)
    n = len(mono) // hop
    return np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1))


def regions_from_ratio(ratio: np.ndarray) -> list[tuple[float, float]]:
    spans, on, start = [], False, 0
    for i, v in enumerate(ratio):
        if not on and v >= ON_RATIO:
            on, start = True, i
        elif on and v < OFF_RATIO:
            on = False
            spans.append((start * HOP_S, i * HOP_S))
    if on:
        spans.append((start * HOP_S, len(ratio) * HOP_S))
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < MERGE_GAP_S:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= MIN_REGION_S]


def main() -> None:
    stems_dirs = {os.path.basename(p) for p in glob.glob("data/state/stems/*") if os.path.isdir(p)}
    cache = {os.path.basename(p)[:-5] for p in glob.glob("data/state/vocal-cache/*.json")}
    db = sqlite3.connect("file:data/state/state.db?mode=ro", uri=True)

    rows = []
    for sid in sorted(stems_dirs & cache):
        bundle = f"data/state/stems/{sid}"
        try:
            envs = {}
            sr = None
            for s in STEMS:
                mono, sr = read_mono(f"{bundle}/{s}.wav")
                envs[s] = (mono, sr)
            n = min(len(m) for m, _ in envs.values())
            voc = envs["vocals"][0][:n]
            mix = sum(envs[s][0][:n] for s in STEMS)
        except Exception as e:
            print(f"SKIP {sid[:8]}: {type(e).__name__} {e}")
            continue

        v_env, m_env = rms_env(voc, sr), rms_env(mix, sr)
        k = min(len(v_env), len(m_env))
        v_env, m_env = v_env[:k], m_env[:k]
        ratio = np.where(m_env > 1e-6, v_env / np.maximum(m_env, 1e-12), 0.0)

        cached = json.load(open(f"data/state/vocal-cache/{sid}.json"))
        spans = regions_from_ratio(ratio)
        dur = k * HOP_S
        cov = 100.0 * sum(e - s for s, e in spans) / dur

        # mix energy percentiles -> where does the ratio sit when the mix is quiet?
        q10 = np.percentile(m_env, 10)
        quiet = ratio[m_env <= q10]
        loud = ratio[m_env > np.percentile(m_env, 50)]
        title = db.execute("select title from tracks where stable_id=?", (sid,)).fetchone()

        rows.append(
            dict(
                sid=sid[:8],
                title=(title[0] if title else "?")[:38],
                cov_cached=cached.get("coverage_pct"),
                cov_recomp=round(cov, 1),
                n_reg_cached=len(cached.get("regions", [])),
                n_reg_recomp=len(spans),
                first_at_0=bool(spans and spans[0][0] == 0.0),
                ratio_quiet_med=round(float(np.median(quiet)), 3),
                ratio_loud_med=round(float(np.median(loud)), 3),
                frac_quiet_over_on=round(float((quiet >= ON_RATIO).mean()), 3),
                mix_q10_dbfs=round(float(20 * np.log10(max(q10, 1e-9))), 1),
            )
        )

    print(f"\n{'sid':9} {'title':38} {'cchd':>5} {'recmp':>6} {'rq':>6} {'rl':>6} {'q>ON':>6} {'q10dB':>7}")
    print("-" * 92)
    for r in rows:
        print(
            f"{r['sid']:9} {r['title']:38} {str(r['cov_cached']):>5} {r['cov_recomp']:>6} "
            f"{r['ratio_quiet_med']:>6} {r['ratio_loud_med']:>6} {r['frac_quiet_over_on']:>6} {r['mix_q10_dbfs']:>7}"
        )

    if rows:
        fq = np.array([r["frac_quiet_over_on"] for r in rows])
        print(f"\nn={len(rows)} bundles")
        print(f"median ratio in QUIETEST decile of mix: {np.median([r['ratio_quiet_med'] for r in rows]):.3f}")
        print(f"median ratio in LOUD half of mix:       {np.median([r['ratio_loud_med'] for r in rows]):.3f}")
        print(f"frac of quiet frames above ON_RATIO 0.10: median {np.median(fq):.3f}, max {fq.max():.3f}")
        d = [(r["cov_recomp"] - r["cov_cached"]) for r in rows if r["cov_cached"] is not None]
        print(f"recomputed-vs-cached coverage delta: median {np.median(d):+.1f} pts, max abs {max(abs(x) for x in d):.1f}")
    json.dump(rows, open("/private/tmp/claude-502/-Users-dev-Music-music-dj-tools/31adbe50-f905-4cda-8e46-3083bd0f8ced/scratchpad/q0_free.json", "w"), indent=1)


main()
