#!/usr/bin/env node
/**
 * Export blind A/B pairs for the echo voicing (issue #9815, FXECHO).
 *
 * Spec: (private path) section 6 Feature B. Renders ONLY through
 * the shipped echo kernel (#9780, TAPE mode #9814) with option overrides, via the #9774 harness adapter,
 * so what the maintainer rates is what ships. Until that adapter exists the CLI prints [UNAVAILABLE], exits 2 and
 * writes nothing. This script holds no echo DSP: it synthesizes the fixture, measures, blinds and writes.
 *
 * Usage:
 *   node scripts/export-echo-rating-pairs.mjs --out <dir> [--seed <uint32>] [--sample-rate <hz>]
 *
 * Mini-PRD:
 *   ✔︎ ✅ 🎯 R1 pairs: feedback at 3/4 beat, division 1/2 vs 3/4, loop tone (6 pairs), tape glide vs crossfade
 *   ✔︎ ✅ 🎯 R2 8 bars at 124 BPM, send open 1 bar then closed, pair sides matched within 0.5 LU
 *   ✔︎ ✅ 🎯 R3 seeded side order, blind_key.json written apart from pairs.json
 *   ✔︎ ✅ 🎯 R4 manifest shape and #9776 schema checked, ManifestSchemaError on a mismatch
 *   ✔︎ ✅ 🎯 R5 listening time printed and under 5 minutes
 */
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  ManifestSchemaError,
  UnavailableError,
  _house124,
  _rng,
  clipSeconds,
  levelMatchPair,
} from "./export-time-fx-rating-pairs.mjs";

export { ManifestSchemaError, UnavailableError };

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9815;
export const SAMPLE_RATE = 48000;
export const BPM = 124;
export const CLIP_BARS = 8;
export const BEATS_PER_BAR = 4;
export const SEND_ON_BEAT = 4;
export const SEND_OFF_BEAT = 8;
export const RETUNE = Object.freeze({ atBeat: 12, toBeat: 0.5 });
export const LUFS_MATCH_MAX_DB = 0.5;
export const LISTENING_MAX_SEC = 300;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const FIXTURE_NAME = "house-124";
export const FAMILY = "echo";
export const QUESTION = "Which echo sounds better?";
export const EXIT_UNAVAILABLE = 2;
/** Shipped color ECHO voicing (color-fx-graph.ts ECHO_FEEDBACK, ECHO_LOOP_HP_HZ, ECHO_LOOP_LP_HZ). */
export const BASE_OPTIONS = Object.freeze({
  mode: "echo",
  beat: 0.75,
  feedback: 0.55,
  loopHpHz: 180,
  loopLpHz: 3000,
});
export const REAL_PATH_DEPENDENCIES = Object.freeze([
  {
    issue: "#9774",
    what: "fx render harness",
    path: "apps/webui/frontend/tests/e2e/support/fx-render-harness.ts",
  },
  {
    issue: "#9780",
    what: "echo kernel",
    path: "apps/webui/frontend/src/lib/rb/fx/echo-kernel.ts",
  },
  {
    issue: "#9776",
    what: "A/B page manifest schema",
    path: "tools/fx-ab/manifest.schema.json",
  },
]);
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));
const GENERATED_BY = "scripts/export-echo-rating-pairs.mjs";
const TONE_HP_HZ = [120, 180, 250];
const TONE_LP_HZ = [2000, 3000, 4500];
const _toneLabel = (hp, lp) => `hp${hp}-lp${lp}`;

export const VARIANTS = Object.freeze({
  feedback: Object.fromEntries(
    [0.45, 0.55, 0.65, 0.75].map((f) => [f.toFixed(2), { feedback: f }]),
  ),
  division: { "1/2": { beat: 0.5 }, "3/4": { beat: 0.75 } },
  tone: Object.fromEntries(
    TONE_HP_HZ.flatMap((hp) =>
      TONE_LP_HZ.map((lp) => [
        _toneLabel(hp, lp),
        { loopHpHz: hp, loopLpHz: lp },
      ]),
    ),
  ),
  tape: {
    "glide-80ms": { mode: "tape", tapeGlideSec: 0.08 },
    "glide-250ms": { mode: "tape", tapeGlideSec: 0.25 },
    crossfade: { mode: "echo" },
  },
});
export const AXES = Object.keys(VARIANTS);
const _pair = (axis, a, b) => ({ key: `${axis}:${a}|${b}`, axis, a, b });
/** Tone grid pruned to 6: each HP against the others at LP 3000, each LP against the others at HP 180. */
export const CANONICAL_PAIRS = Object.freeze(
  [
    _pair("feedback", "0.45", "0.55"),
    _pair("feedback", "0.55", "0.65"),
    _pair("feedback", "0.65", "0.75"),
    _pair("division", "1/2", "3/4"),
    _pair("tone", _toneLabel(120, 3000), _toneLabel(180, 3000)),
    _pair("tone", _toneLabel(180, 3000), _toneLabel(250, 3000)),
    _pair("tone", _toneLabel(120, 3000), _toneLabel(250, 3000)),
    _pair("tone", _toneLabel(180, 2000), _toneLabel(180, 3000)),
    _pair("tone", _toneLabel(180, 3000), _toneLabel(180, 4500)),
    _pair("tone", _toneLabel(180, 2000), _toneLabel(180, 4500)),
    _pair("tape", "glide-80ms", "glide-250ms"),
    _pair("tape", "glide-80ms", "crossfade"),
    _pair("tape", "glide-250ms", "crossfade"),
  ].map(Object.freeze),
);
export const PAIR_COUNT = CANONICAL_PAIRS.length;

export class MissingVariantError extends Error {
  /** @param {string} axis @param {string} variant @param {string[]} supported */
  constructor(axis, variant, supported) {
    super(
      `MissingVariantError: echo renderer lacks an option of ${axis} variant ${variant}; supported ${supported.join(",")}`,
    );
    Object.assign(this, { axis, variant, supported });
    this.name = "MissingVariantError";
  }
}
export class VetoError extends Error {
  /** @param {{veto:string,file:string,variant:string,measured:number,limit:number|string}} f */
  constructor(f) {
    super(
      `veto ${f.veto} failed: file=${f.file} variant=${f.variant} measured=${f.measured} limit=${f.limit}`,
    );
    Object.assign(this, f);
    this.name = "VetoError";
  }
}

//----------------------------------------------------------------------------- real render path
/** Return the #9774 real-path echo renderer, or throw UnavailableError naming each missing dependency. */
export function resolveRealRenderer(repoRoot = REPO_ROOT) {
  const missing = REAL_PATH_DEPENDENCIES.filter(
    (d) => !existsSync(join(repoRoot, d.path)),
  );
  if (missing.length > 0) {
    const names = missing
      .map((d) => `${d.issue} ${d.what} (${d.path})`)
      .join(", ");
    throw new UnavailableError(
      `UNAVAILABLE: echo calibration renders only through the shipped kernel; missing ${names}`,
      missing,
    );
  }
  throw new UnavailableError(
    "UNAVAILABLE: dependencies are present but this script has no harness adapter yet; wire resolveRealRenderer",
    [],
  );
}

//----------------------------------------------------------------------------- helpers
const _round = (x, dp = 2) => Math.round(x * 10 ** dp) / 10 ** dp + 0;
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);

/** Session listening time, one clip length per pair (the sibling exporters' measure). */
export function listeningSeconds(pairCount = PAIR_COUNT, bpm = BPM) {
  return _round(pairCount * clipSeconds(bpm), 2);
}
/** Kernel options for one variant: the shipped base voicing plus that variant's overrides. */
export function variantOptions(axis, variant) {
  const table = VARIANTS[axis];
  if (!table || !Object.hasOwn(table, variant))
    throw new MissingVariantError(axis, variant, Object.keys(table ?? {}));
  return { ...BASE_OPTIONS, ...table[variant] };
}

function _assertVariantSupported(axis, variant, renderer) {
  const keys = Object.keys(variantOptions(axis, variant));
  const supported = renderer.supportedOptions("echo");
  if (keys.some((k) => !supported.has(k)))
    throw new MissingVariantError(axis, variant, [...supported]);
}

function _normalisedFixture(channels) {
  let sum = 0;
  for (const ch of channels) for (const x of ch) sum += x * x;
  const gain =
    10 ** (DRY_RMS_DBFS / 20) / Math.sqrt(sum / (2 * channels[0].length));
  const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
  const peakDb = _round(20 * Math.log10(_peak(scaled)), 2);
  if (peakDb > FIXTURE_PEAK_MAX_DBFS)
    throw new Error(
      `fixture ${FIXTURE_NAME} peaks at ${peakDb} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`,
    );
  return { channels: scaled, peak_dbfs: peakDb };
}
function _peak(channels) {
  let peak = 0;
  for (const ch of channels)
    for (const x of ch) peak = Math.max(peak, Math.abs(x));
  return peak;
}
function _vetoFiniteAndPeak(channels, file, variant) {
  for (const ch of channels)
    for (const x of ch)
      if (!Number.isFinite(x))
        throw new VetoError({
          veto: "finite",
          file,
          variant,
          measured: x,
          limit: "finite",
        });
  const peak = _peak(channels);
  if (peak > 1)
    throw new VetoError({
      veto: "peak",
      file,
      variant,
      measured: peak,
      limit: 1,
    });
}
function _encodeWav(channels, sr) {
  const n = channels[0].length;
  const buf = Buffer.alloc(44 + n * 4);
  buf.write("RIFF", 0);
  buf.writeUInt32LE(36 + n * 4, 4);
  buf.write("WAVEfmt ", 8);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(2, 22);
  buf.writeUInt32LE(sr, 24);
  buf.writeUInt32LE(sr * 4, 28);
  buf.writeUInt16LE(4, 32);
  buf.writeUInt16LE(16, 34);
  buf.write("data", 36);
  buf.writeUInt32LE(n * 4, 40);
  for (let i = 0; i < n; i++)
    for (let c = 0; c < 2; c++)
      buf.writeInt16LE(Math.round(channels[c][i] * 32767), 44 + i * 4 + c * 2);
  return buf;
}

//----------------------------------------------------------------------------- manifest
/** Throw ManifestSchemaError naming the first field where `manifest` breaks the echo pairs shape or #9776 schema. */
export function validateManifestSchema(manifest) {
  const fail = (field) => {
    throw new ManifestSchemaError(`echo pairs manifest: ${field}`);
  };
  if (manifest?.family !== FAMILY) fail(`family must be ${FAMILY}`);
  if (!Number.isInteger(manifest.round) || manifest.round < 1)
    fail("round must be an integer >= 1");
  if (
    !Number.isInteger(manifest.seed) ||
    manifest.seed < 0 ||
    manifest.seed > 0xffffffff
  )
    fail("seed must be a uint32");
  if (
    typeof manifest.renderer?.id !== "string" ||
    typeof manifest.renderer.real_path !== "boolean"
  )
    fail("renderer must be {id, real_path}");
  if (!manifest.hidden_fields?.includes("meta"))
    fail("hidden_fields must include meta");
  if (!Array.isArray(manifest.pairs) || manifest.pairs.length !== PAIR_COUNT)
    fail(`pairs must hold ${PAIR_COUNT} items`);
  if (!(manifest.listening_seconds <= LISTENING_MAX_SEC))
    fail("listening_seconds over budget");
  const ids = new Set();
  for (const p of manifest.pairs) {
    for (const key of ["id", "a", "b", "question"])
      if (typeof p?.[key] !== "string" || !p[key])
        fail(`pair missing required string ${key}`);
    if (ids.has(p.id)) fail(`pair id ${p.id} must be unique`);
    ids.add(p.id);
    if (
      ![p.a, p.b].every((s) => s.startsWith("clips/") && s.endsWith(".wav")) ||
      p.a === p.b
    )
      fail(`pair ${p.id} sides must be two distinct clips/*.wav`);
    const m = p.meta;
    if (!AXES.includes(m?.axis)) fail(`pair ${p.id} meta.axis`);
    if (
      m.a_variant === m.b_variant ||
      ![m.a_variant, m.b_variant].every((v) =>
        Object.hasOwn(VARIANTS[m.axis], v),
      )
    )
      fail(`pair ${p.id} variants`);
    if (!(m.lufs_delta_db <= LUFS_MATCH_MAX_DB))
      fail(`pair ${p.id} lufs_delta_db`);
    if (m.send_on_beat !== SEND_ON_BEAT || m.send_off_beat !== SEND_OFF_BEAT)
      fail(`pair ${p.id} send gate beats`);
  }
}

//----------------------------------------------------------------------------- export
/**
 * Render, level-match, blind and write the echo pairs to `outDir`; return the manifest.
 * @param {{outDir:string, renderer:{id:string, realPath:boolean, supportedOptions(kernel:string):Set<string>,
 *   render(req:object):Promise<Float64Array[]>}, allowTestDouble?:boolean, seed?:number, sampleRate?:number}} opts
 */
export async function exportEchoRatingPairs({
  outDir,
  renderer,
  allowTestDouble = false,
  seed = DEFAULT_SEED,
  sampleRate = SAMPLE_RATE,
}) {
  if (
    typeof renderer?.render !== "function" ||
    typeof renderer.supportedOptions !== "function"
  )
    throw new Error(
      "renderer {id, realPath, render, supportedOptions} is required",
    );
  if (renderer.realPath !== true && !allowTestDouble)
    throw new Error(
      `renderer ${renderer.id} is not a real-path renderer; pass allowTestDouble only from tests`,
    );
  if (!outDir) throw new Error("--out <dir> is required");
  if (existsSync(outDir) && readdirSync(outDir).length > 0)
    throw new Error(
      `--out ${outDir} is not empty; refusing to overwrite an earlier rating round`,
    );
  seed >>>= 0;
  const sr = sampleRate;
  const n = _beatToSample(CLIP_BARS * BEATS_PER_BAR, BPM, sr);
  const fixture = _normalisedFixture(_house124(n, sr, BPM, seed));
  const request = {
    kernel: "echo",
    input: fixture.channels,
    sampleRate: sr,
    bpm: BPM,
    seed,
    sendOnSample: _beatToSample(SEND_ON_BEAT, BPM, sr),
    sendOffSample: _beatToSample(SEND_OFF_BEAT, BPM, sr),
  };

  const order = CANONICAL_PAIRS.map((p) => ({ ...p }));
  const next = _rng(seed);
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  const cache = new Map();
  const clips = [];
  const pairs = [];
  const blindKey = { seed, pairs: [] };
  for (const [idx, spec] of order.entries()) {
    const id = `p${String(idx + 1).padStart(2, "0")}`;
    const [aVar, bVar] = next() < 0.5 ? [spec.b, spec.a] : [spec.a, spec.b];
    const sides = [];
    for (const variant of [aVar, bVar]) {
      _assertVariantSupported(spec.axis, variant, renderer);
      const key = `${spec.axis}|${variant}`;
      if (!cache.has(key)) {
        const retune = spec.axis === "tape" ? RETUNE : null;
        const out = await renderer.render({
          ...request,
          axis: spec.axis,
          variant,
          options: variantOptions(spec.axis, variant),
          retune,
        });
        if (
          !Array.isArray(out) ||
          out.length !== 2 ||
          out.some((ch) => ch.length !== n)
        )
          throw new Error(
            `renderer ${renderer.id} broke the contract for ${key}`,
          );
        _vetoFiniteAndPeak(out, `clips/${id}-raw.wav`, variant);
        cache.set(key, out);
      }
      sides.push(cache.get(key));
    }
    const matched = levelMatchPair(sides[0], sides[1], sr);
    const a = `clips/${id}-a.wav`;
    const b = `clips/${id}-b.wav`;
    _vetoFiniteAndPeak(matched.a, a, aVar);
    _vetoFiniteAndPeak(matched.b, b, bVar);
    clips.push([a, matched.a], [b, matched.b]);
    pairs.push({
      id,
      a,
      b,
      question: QUESTION,
      meta: {
        key: spec.key,
        axis: spec.axis,
        fixture: FIXTURE_NAME,
        send_on_beat: SEND_ON_BEAT,
        send_off_beat: SEND_OFF_BEAT,
        a_variant: aVar,
        b_variant: bVar,
        options: {
          a: variantOptions(spec.axis, aVar),
          b: variantOptions(spec.axis, bVar),
        },
        lufs_a: matched.lufs_a,
        lufs_b: matched.lufs_b,
        lufs_delta_db: matched.lufs_delta_db,
        match_gain_db: matched.match_gain_db,
      },
    });
    blindKey.pairs.push({ id, a_variant: aVar, b_variant: bVar, a, b });
  }

  const manifest = {
    family: FAMILY,
    round: 1,
    seed,
    generated_by: GENERATED_BY,
    renderer: { id: renderer.id, real_path: renderer.realPath },
    sample_rate: sr,
    bpm: BPM,
    bars: CLIP_BARS,
    samples: n,
    lufs_match_max_db: LUFS_MATCH_MAX_DB,
    listening_seconds: listeningSeconds(pairs.length),
    fixtures: [
      {
        name: FIXTURE_NAME,
        source: "synthesized",
        dry_rms_dbfs: DRY_RMS_DBFS,
        peak_dbfs: fixture.peak_dbfs,
      },
    ],
    base_options: BASE_OPTIONS,
    retune: RETUNE,
    variants: VARIANTS,
    vetoes_applied: ["finite", "peak"],
    hidden_fields: ["meta"],
    pairs,
  };
  validateManifestSchema(manifest);

  mkdirSync(join(outDir, "clips"), { recursive: true });
  for (const [rel, channels] of clips)
    writeFileSync(join(outDir, rel), _encodeWav(channels, sr));
  writeFileSync(
    join(outDir, "pairs.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  writeFileSync(
    join(outDir, "blind_key.json"),
    `${JSON.stringify(blindKey, null, 2)}\n`,
  );
  return manifest;
}

//----------------------------------------------------------------------------- CLI
function _parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, val] = [argv[i], argv[i + 1]];
    if (val === undefined) throw new Error(`${flag} needs a value`);
    if (flag === "--out") opts.outDir = val;
    else if (flag === "--seed") opts.seed = Number(val);
    else if (flag === "--sample-rate") opts.sampleRate = Number(val);
    else throw new Error(`unknown argument ${flag}`);
  }
  if (
    "seed" in opts &&
    !(Number.isInteger(opts.seed) && opts.seed >= 0 && opts.seed <= 0xffffffff)
  )
    throw new Error("invalid --seed");
  if (
    "sampleRate" in opts &&
    !(Number.isInteger(opts.sampleRate) && opts.sampleRate > 0)
  )
    throw new Error("invalid --sample-rate");
  if (!opts.outDir) throw new Error("--out <dir> is required");
  return opts;
}

/** CLI entry: 0 written, 1 bad arguments or a failed export, 2 UNAVAILABLE (no real render path). */
export async function main(argv) {
  let opts;
  try {
    opts = _parseArgs(argv);
  } catch (err) {
    console.error(`[ERROR] ${err.message}`);
    return 1;
  }
  console.log(
    `[INFO] listening time ${listeningSeconds()} s for ${PAIR_COUNT} pairs (budget ${LISTENING_MAX_SEC} s)`,
  );
  let renderer;
  try {
    renderer = resolveRealRenderer();
  } catch (err) {
    if (!(err instanceof UnavailableError)) throw err;
    console.error(`[UNAVAILABLE] ${err.message}`);
    return EXIT_UNAVAILABLE;
  }
  try {
    const m = await exportEchoRatingPairs({ ...opts, renderer });
    console.log(
      `[OK] wrote ${m.pairs.length} pairs, listening ${m.listening_seconds} s, manifest ${join(opts.outDir, "pairs.json")}, seed ${m.seed}`,
    );
    return 0;
  } catch (err) {
    console.error(`[ERROR] ${err.message}`);
    return 1;
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  process.exitCode = await main(process.argv.slice(2));
}
