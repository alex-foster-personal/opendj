#!/usr/bin/env node
/**
 * Export blind A/B pairs for time-based beat FX variants (issue #10019).
 *
 * Pattern: #9815 echo voicing export (clip law, blinding, manifest). Renders ONLY through the
 * shipped kernels with option overrides (#9992 width, #10003 spiral density, #10009 multitap law,
 * #10011 tape motion) once the #9774 harness adapter exists. Until then: [UNAVAILABLE], exit 2.
 *
 * Usage:
 *   node scripts/export-time-fx-rating-pairs.mjs --out <dir> [--seed <uint32>] [--sample-rate <hz>]
 *       [--fixture house-124=<path.wav>]
 */
import {
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
  existsSync,
} from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

/**
 * @typedef {{ kernel: string, variant: string, options: object, beat?: number, depth?: number,
 *   input: Float64Array[], sampleRate: number, bpm: number, seed: number,
 *   sendOnSample: number, sendOffSample: number }} RenderRequest
 * @typedef {{ id: string, realPath: boolean, supportedOptions(kernel: string): Set<string>,
 *   render(req: RenderRequest): Promise<Float64Array[]> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed0019;
export const SAMPLE_RATE = 48000;
export const BPM = 124;
export const CLIP_BARS = 8;
export const BEATS_PER_BAR = 4;
export const SEND_ON_BEAT = 4;
export const SEND_OFF_BEAT = 8;
export const SPIRAL_DEPTH = 0.7;
export const LUFS_MATCH_MAX_DB = 0.5;
export const LISTENING_MAX_SEC = 240;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const FIXTURE_NAME = "house-124";
export const FAMILY = "time-fx";
export const QUESTION = "Which time FX sounds better?";
export const EXIT_UNAVAILABLE = 2;
export const MAX_PAIRS = 10;
export const REAL_PATH_DEPENDENCIES = Object.freeze([
  {
    issue: "#9774",
    what: "fx render harness",
    path: "apps/webui/frontend/tests/e2e/support/fx-render-harness.ts",
  },
  {
    issue: "#9780",
    what: "echo kernel (pingpong and tape)",
    path: "apps/webui/frontend/src/lib/rb/fx/echo-kernel.ts",
  },
  {
    issue: "#10003",
    what: "SPIRAL kernel",
    path: "apps/webui/frontend/src/lib/rb/fx/spiral-kernel.ts",
  },
  {
    issue: "#10009",
    what: "multi-tap kernel",
    path: "apps/webui/frontend/src/lib/rb/fx/multitap-kernel.ts",
  },
  {
    issue: "#10011",
    what: "tape motion",
    path: "apps/webui/frontend/src/lib/rb/fx/tape-motion.ts",
  },
  {
    issue: "#9776",
    what: "A/B page manifest schema",
    path: "tools/fx-ab/manifest.schema.json",
  },
]);
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));
const GENERATED_BY = "scripts/export-time-fx-rating-pairs.mjs";
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
export const KERNELS = ["pingpong", "spiral", "multitap", "tape"];
export const VARIANTS = Object.freeze({
  pingpong: {
    0.5: { pingpongWidth: 0.5 },
    "1.0": { pingpongWidth: 1.0 },
  },
  spiral: {
    light: { spiralVariant: "light" },
    medium: { spiralVariant: "medium" },
    dense: { spiralVariant: "dense" },
  },
  multitap: {
    flat: { multitapLaw: "flat" },
    decay: { multitapLaw: "decay" },
  },
  tape: {
    off: { tapeMotion: "off" },
    subtle: { tapeMotion: "subtle" },
    heavy: { tapeMotion: "heavy" },
  },
});
export const CANONICAL_PAIRS = Object.freeze(
  [
    {
      key: "pp-w-12",
      kernel: "pingpong",
      axis: "width",
      a: "0.5",
      b: "1.0",
      beat: 0.5,
    },
    {
      key: "pp-w-34",
      kernel: "pingpong",
      axis: "width",
      a: "0.5",
      b: "1.0",
      beat: 0.75,
    },
    {
      key: "sp-lm",
      kernel: "spiral",
      axis: "density",
      a: "light",
      b: "medium",
      depth: SPIRAL_DEPTH,
    },
    {
      key: "sp-md",
      kernel: "spiral",
      axis: "density",
      a: "medium",
      b: "dense",
      depth: SPIRAL_DEPTH,
    },
    {
      key: "sp-ld",
      kernel: "spiral",
      axis: "density",
      a: "light",
      b: "dense",
      depth: SPIRAL_DEPTH,
    },
    {
      key: "mt-1",
      kernel: "multitap",
      axis: "law",
      a: "flat",
      b: "decay",
      beat: 1,
    },
    {
      key: "mt-h",
      kernel: "multitap",
      axis: "law",
      a: "flat",
      b: "decay",
      beat: 0.5,
    },
    { key: "tp-os", kernel: "tape", axis: "motion", a: "off", b: "subtle" },
    { key: "tp-sh", kernel: "tape", axis: "motion", a: "subtle", b: "heavy" },
    { key: "tp-oh", kernel: "tape", axis: "motion", a: "off", b: "heavy" },
  ].map(Object.freeze),
);
const VETOES_NOT_APPLIED = [
  {
    veto: "added-latency",
    reason: "measured by the #9774 harness in each kernel's own PR",
  },
  {
    veto: "click-ratio",
    reason: "measured by the #9774 harness in each kernel's own PR",
  },
  {
    veto: "tail-decay",
    reason: "measured by the #9774 harness in each kernel's own PR",
  },
];

export class VetoError extends Error {
  /** @param {{veto:string,file:string,kernel:string,variant:string,measured:number,limit:number|string}} f */
  constructor(f) {
    super(
      `veto ${f.veto} failed: file=${f.file} kernel=${f.kernel} variant=${f.variant} measured=${f.measured} limit=${f.limit}`,
    );
    Object.assign(this, f);
    this.name = "VetoError";
  }
}
export class UnavailableError extends Error {
  /** @param {string} reason @param {typeof REAL_PATH_DEPENDENCIES[number][]} missing */
  constructor(reason, missing) {
    super(reason);
    this.reason = reason;
    this.missing = missing;
    this.name = "UnavailableError";
  }
}
export class MissingVariantError extends Error {
  /** @param {string} kernel @param {string} variant @param {string[]} supported */
  constructor(kernel, variant, supported) {
    super(
      `MissingVariantError: kernel ${kernel} lacks variant option ${variant}; supported ${supported.join(",")}`,
    );
    this.kernel = kernel;
    this.variant = variant;
    this.supported = supported;
    this.name = "MissingVariantError";
  }
}
export class ManifestSchemaError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = "ManifestSchemaError";
  }
}

//----------------------------------------------------------------------------- real render path
export function resolveRealRenderer(repoRoot = REPO_ROOT) {
  const missing = REAL_PATH_DEPENDENCIES.filter(
    (d) => !existsSync(join(repoRoot, d.path)),
  );
  if (missing.length > 0) {
    const names = missing
      .map((d) => `${d.issue} ${d.what} (${d.path})`)
      .join(", ");
    throw new UnavailableError(
      `UNAVAILABLE: time-fx calibration renders only through the real kernels; missing ${names}`,
      missing,
    );
  }
  throw new UnavailableError(
    "UNAVAILABLE: dependencies are present but this script has no harness adapter yet; wire resolveRealRenderer",
    [],
  );
}

//----------------------------------------------------------------------------- helpers
function _rng(seed) {
  let state = seed >>> 0 || 1;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 0x100000000;
  };
}
const _dbToLin = (db) => 10 ** (db / 20);
const _round = (x, dp = 2) => Math.round(x * 10 ** dp) / 10 ** dp + 0;
function _rms(channels, from = 0, to = channels[0].length) {
  let sum = 0;
  let n = 0;
  for (const ch of channels)
    for (let i = from; i < to; i++) {
      sum += ch[i] * ch[i];
      n++;
    }
  return Math.sqrt(sum / n);
}
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);
function _shuffle(items, next) {
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
}

/** ITU-R BS.1770 short-term LUFS (3 s gated window, K-weighted stereo sum). */
export function shortTermLufs(channels, sr) {
  const len = channels[0].length;
  const win = Math.min(len, Math.max(1, Math.round(3 * sr)));
  const step = Math.max(1, Math.round(0.1 * sr));
  const hp = 0.996;
  let maxMean = 0;
  const starts = len <= win ? [0] : [];
  for (let start = 0; start + win <= len; start += step) starts.push(start);
  for (const start of starts) {
    let mean = 0;
    for (const ch of channels) {
      let z1 = 0;
      let z2 = 0;
      for (let i = start; i < start + win; i++) {
        const x = ch[i];
        z1 = hp * (z1 + x - (i > start ? ch[i - 1] : 0));
        const y = z1 - z2;
        z2 = hp * (z2 + y);
        const k = 1.53 * y - 0.45 * z1;
        mean += k * k;
      }
    }
    mean /= 2 * win;
    maxMean = Math.max(maxMean, mean);
  }
  if (maxMean <= 0) return -Infinity;
  return -0.691 + 10 * Math.log10(maxMean);
}

export function levelMatchPair(aIn, bIn, sr) {
  let lufsA = shortTermLufs(aIn, sr);
  let lufsB = shortTermLufs(bIn, sr);
  if (!Number.isFinite(lufsA) || !Number.isFinite(lufsB)) {
    throw new Error("short-term LUFS is non-finite on a pair side");
  }
  const mid = (lufsA + lufsB) / 2;
  const gainDbA = _round(mid - lufsA, 2);
  const gainDbB = _round(mid - lufsB, 2);
  const aOut = aIn.map((ch) =>
    Float64Array.from(ch, (x) => x * _dbToLin(gainDbA)),
  );
  const bOut = bIn.map((ch) =>
    Float64Array.from(ch, (x) => x * _dbToLin(gainDbB)),
  );
  lufsA = shortTermLufs(aOut, sr);
  lufsB = shortTermLufs(bOut, sr);
  return {
    a: aOut,
    b: bOut,
    lufs_a: _round(lufsA, 2),
    lufs_b: _round(lufsB, 2),
    match_gain_db: { a: gainDbA, b: gainDbB },
    lufs_delta_db: _round(Math.abs(lufsA - lufsB), 3),
  };
}

export function clipSeconds(bpm = BPM, bars = CLIP_BARS) {
  return (bars * BEATS_PER_BAR * 60) / bpm;
}
export function listeningSeconds(
  pairCount = MAX_PAIRS,
  bpm = BPM,
  bars = CLIP_BARS,
) {
  return _round((pairCount * bars * BEATS_PER_BAR * 60) / bpm, 2);
}

function _assertVariantSupported(kernel, variant, renderer) {
  const table = VARIANTS[kernel];
  if (!table || !(variant in table)) {
    throw new MissingVariantError(kernel, variant, Object.keys(table ?? {}));
  }
  const supported =
    renderer.supportedOptions?.(kernel) ?? new Set(Object.keys(table));
  for (const key of Object.keys(table[variant])) {
    if (!supported.has(key)) {
      throw new MissingVariantError(kernel, variant, [...supported]);
    }
  }
}

//----------------------------------------------------------------------------- fixture (#9815 house-124 loop)
function _house124(n, sr, bpm, seed) {
  const beat = (60 / bpm) * sr;
  const chord = [130.81, 164.81, 196];
  const out = [new Float64Array(n), new Float64Array(n)];
  out.forEach((buf, c) => {
    const next = _rng(seed ^ (0x1000 + c));
    let bassPh = 0;
    const chordPh = chord.map(() => 0);
    for (let i = 0; i < n; i++) {
      const inBeat = (i % beat) / sr;
      const pos = (i / beat) % 1;
      const kickPh =
        2 * Math.PI * (50 * inBeat + 4 * (1 - Math.exp(-inBeat / 0.04)));
      let x = Math.sin(kickPh) * Math.exp(-inBeat / 0.25);
      if (pos >= 0.5 && pos < 0.5 + 0.03 / (60 / bpm))
        x += 0.15 * (next() * 2 - 1) * Math.exp(-(pos - 0.5) * 20);
      bassPh += (2 * Math.PI * 55) / sr;
      if (pos >= 0.5) x += 0.4 * Math.sin(bassPh);
      let saw = 0;
      chord.forEach((f, k) => {
        chordPh[k] += (2 * Math.PI * f * (1 + 0.002 * c)) / sr;
        for (let h = 1; h <= 6; h++) saw += Math.sin(h * chordPh[k]) / h;
      });
      buf[i] = x + 0.12 * saw;
    }
  });
  return out;
}
function _normaliseFixture(name, source, channels) {
  const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
  const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
  let peak = 0;
  for (const ch of scaled)
    for (const x of ch) peak = Math.max(peak, Math.abs(x));
  const peakDb = _round(20 * Math.log10(peak), 2);
  if (peakDb > FIXTURE_PEAK_MAX_DBFS) {
    throw new Error(
      `fixture ${name} peaks at ${peakDb} dBFS after RMS normalisation to ${DRY_RMS_DBFS} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`,
    );
  }
  return {
    name,
    source,
    channels: scaled,
    dry_rms_dbfs: DRY_RMS_DBFS,
    peak_dbfs: peakDb,
  };
}
function _buildFixture(n, sr, bpm, seed, overrides) {
  const path = overrides?.[FIXTURE_NAME];
  if (path)
    return _normaliseFixture(FIXTURE_NAME, "wav", _readWav(path, sr, n));
  return _normaliseFixture(
    FIXTURE_NAME,
    "synthesized",
    _house124(n, sr, bpm, seed),
  );
}

//----------------------------------------------------------------------------- vetoes
function _checkFiniteAndPeak(channels, ctx) {
  for (const ch of channels) {
    for (const x of ch) {
      const f = Math.fround(x);
      if (!Number.isFinite(f) || (f !== 0 && Math.abs(f) < FLT_MIN_NORMAL))
        throw new VetoError({
          veto: "finite",
          ...ctx,
          measured: f,
          limit: "finite normal float32",
        });
    }
  }
  for (const ch of channels)
    for (const x of ch)
      if (Math.abs(x) > 1)
        throw new VetoError({
          veto: "peak",
          ...ctx,
          measured: Math.abs(x),
          limit: 1,
        });
}

//----------------------------------------------------------------------------- manifest
/** @param {any} manifest */
export function assertTimeFxPairsManifestShape(manifest) {
  const fail = (field) => {
    throw new Error(`time-fx pairs manifest shape: ${field}`);
  };
  if (manifest?.family !== FAMILY) fail(`family must be ${FAMILY}`);
  if (!Number.isInteger(manifest?.round) || manifest.round < 1)
    fail("round must be an integer >= 1");
  if (
    !Number.isInteger(manifest?.seed) ||
    manifest.seed < 0 ||
    manifest.seed > 0xffffffff
  )
    fail("seed must be a uint32");
  if (
    typeof manifest?.renderer?.id !== "string" ||
    !manifest.renderer.id ||
    typeof manifest.renderer.real_path !== "boolean"
  )
    fail("renderer must be {id, real_path}");
  if (
    !Array.isArray(manifest?.hidden_fields) ||
    !manifest.hidden_fields.includes("meta")
  )
    fail("hidden_fields must include meta");
  if (!Array.isArray(manifest?.pairs) || manifest.pairs.length !== MAX_PAIRS)
    fail(`pairs must hold ${MAX_PAIRS} items`);
  if (
    !Number.isFinite(manifest?.listening_seconds) ||
    manifest.listening_seconds > LISTENING_MAX_SEC
  )
    fail("listening_seconds must be finite and within the session budget");
  const ids = new Set();
  for (const p of manifest.pairs) {
    if (typeof p.id !== "string" || !p.id || ids.has(p.id))
      fail(`pair id ${p.id} must be unique`);
    ids.add(p.id);
    for (const side of ["a", "b"]) {
      if (
        typeof p[side] !== "string" ||
        !p[side].startsWith("clips/") ||
        !p[side].endsWith(".wav")
      )
        fail(`pair ${p.id} ${side} must be clips/*.wav`);
    }
    if (p.a === p.b) fail(`pair ${p.id} a/b must differ`);
    if (p.question !== QUESTION) fail(`pair ${p.id} question`);
    const m = p.meta;
    if (!m || !KERNELS.includes(m.kernel)) fail(`pair ${p.id} meta.kernel`);
    if (m.a_variant === m.b_variant) fail(`pair ${p.id} variants must differ`);
    const table = manifest.variants?.[m.kernel] ?? VARIANTS[m.kernel];
    if (!(m.a_variant in table) || !(m.b_variant in table))
      fail(`pair ${p.id} unknown variants`);
    if (
      !Number.isFinite(m.lufs_delta_db) ||
      m.lufs_delta_db > LUFS_MATCH_MAX_DB + 1e-6
    )
      fail(`pair ${p.id} lufs_delta_db`);
    if (m.send_on_beat !== SEND_ON_BEAT || m.send_off_beat !== SEND_OFF_BEAT)
      fail(`pair ${p.id} send gate beats`);
  }
}

export function validateManifestSchema(manifest, repoRoot = REPO_ROOT) {
  try {
    assertTimeFxPairsManifestShape(manifest);
  } catch (err) {
    throw new ManifestSchemaError(
      err instanceof Error ? err.message : String(err),
    );
  }
  const schemaPath = join(repoRoot, "tools/fx-ab/manifest.schema.json");
  if (!existsSync(schemaPath)) return;
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  const required = schema.required ?? ["family", "round", "pairs"];
  for (const key of required) {
    if (!(key in manifest))
      throw new ManifestSchemaError(`manifest missing required field ${key}`);
  }
  if (!Array.isArray(manifest.pairs))
    throw new ManifestSchemaError("manifest.pairs must be an array");
  for (const p of manifest.pairs) {
    for (const key of ["id", "a", "b", "question"]) {
      if (typeof p?.[key] !== "string" || !p[key]) {
        throw new ManifestSchemaError(`pair missing required string ${key}`);
      }
    }
  }
}

//----------------------------------------------------------------------------- WAV IO
function _encodeWav(channels, sr) {
  const n = channels[0].length;
  const buf = Buffer.alloc(44 + n * 4);
  buf.write("RIFF", 0);
  buf.writeUInt32LE(36 + n * 4, 4);
  buf.write("WAVE", 8);
  buf.write("fmt ", 12);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(2, 22);
  buf.writeUInt32LE(sr, 24);
  buf.writeUInt32LE(sr * 4, 28);
  buf.writeUInt16LE(4, 32);
  buf.writeUInt16LE(16, 34);
  buf.write("data", 36);
  buf.writeUInt32LE(n * 4, 40);
  for (let i = 0; i < n; i++) {
    for (let c = 0; c < 2; c++)
      buf.writeInt16LE(
        Math.round(Math.max(-1, Math.min(1, channels[c][i])) * 32767),
        44 + i * 4 + c * 2,
      );
  }
  return buf;
}
function _readWav(path, sr, n) {
  const buf = readFileSync(path);
  if (
    buf.toString("ascii", 0, 4) !== "RIFF" ||
    buf.toString("ascii", 8, 12) !== "WAVE"
  )
    throw new Error(`${path} is not RIFF/WAVE`);
  let off = 12;
  let fmt = null;
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === "fmt ")
      fmt = {
        format: buf.readUInt16LE(off + 8),
        ch: buf.readUInt16LE(off + 10),
        rate: buf.readUInt32LE(off + 12),
        bits: buf.readUInt16LE(off + 22),
      };
    else if (id === "data") {
      if (!fmt) throw new Error(`${path} has data before fmt`);
      const kind = `${fmt.format}/${fmt.bits}`;
      if (!["1/16", "1/24", "3/32"].includes(kind) || fmt.ch < 1 || fmt.ch > 2)
        throw new Error(`${path} unsupported format ${kind}`);
      if (fmt.rate !== sr)
        throw new Error(`${path} sample rate ${fmt.rate} != ${sr}`);
      const bytes = fmt.bits / 8;
      const frames = Math.floor(size / (bytes * fmt.ch));
      if (frames < n)
        throw new Error(`${path} has ${frames} frames, needs ${n}`);
      const read = (p) =>
        kind === "1/16"
          ? buf.readInt16LE(p) / 32768
          : kind === "1/24"
            ? buf.readIntLE(p, 3) / 8388608
            : buf.readFloatLE(p);
      return [0, 1].map((c) =>
        Float64Array.from({ length: n }, (_, i) =>
          read(off + 8 + (i * fmt.ch + Math.min(c, fmt.ch - 1)) * bytes),
        ),
      );
    }
    off += 8 + size + (size % 2);
  }
  throw new Error(`${path} has no data chunk`);
}

//----------------------------------------------------------------------------- export
const _renderKey = (spec, variant) =>
  `${spec.kernel}|${spec.beat ?? ""}|${spec.depth ?? ""}|${variant}`;

/**
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, sampleRate?:number, bpm?:number, fixtures?:Record<string,string>}} opts
 */
export async function exportTimeFxRatingPairs({
  outDir,
  renderer,
  allowTestDouble = false,
  seed = DEFAULT_SEED,
  sampleRate = SAMPLE_RATE,
  bpm = BPM,
  fixtures,
}) {
  if (
    typeof renderer?.id !== "string" ||
    typeof renderer.realPath !== "boolean" ||
    typeof renderer.render !== "function"
  )
    throw new Error(
      "renderer {id, realPath, render, supportedOptions} is required",
    );
  if (renderer.realPath !== true && !allowTestDouble) {
    throw new Error(
      `renderer ${renderer.id} is not a real-path renderer; pass allowTestDouble only from tests`,
    );
  }
  if (!outDir) throw new Error("--out <dir> is required");
  if (existsSync(outDir) && readdirSync(outDir).length > 0)
    throw new Error(
      `--out ${outDir} is not empty; refusing to overwrite an earlier rating round`,
    );
  seed >>>= 0;
  const sr = sampleRate;
  const n = _beatToSample(CLIP_BARS * BEATS_PER_BAR, bpm, sr);
  const sendOnSample = _beatToSample(SEND_ON_BEAT, bpm, sr);
  const sendOffSample = _beatToSample(SEND_OFF_BEAT, bpm, sr);
  const fixture = _buildFixture(n, sr, bpm, seed, fixtures);
  const dry = fixture.channels;

  const canonical = CANONICAL_PAIRS.map((p) => ({ ...p }));
  const next = _rng(seed);
  _shuffle(canonical, next);
  canonical.forEach((c, idx) => {
    c.id = `p${String(idx + 1).padStart(2, "0")}`;
    c.swap = next() < 0.5;
  });

  const cache = new Map();
  const clips = [];
  const pairs = [];
  const blindKey = { seed, pairs: [] };

  for (const c of canonical) {
    const [aVar, bVar] = c.swap ? [c.b, c.a] : [c.a, c.b];
    _assertVariantSupported(c.kernel, aVar, renderer);
    _assertVariantSupported(c.kernel, bVar, renderer);
    const rendered = {};
    for (const variant of [aVar, bVar]) {
      const key = _renderKey(c, variant);
      if (!cache.has(key)) {
        const options = { ...VARIANTS[c.kernel][variant] };
        const out = await renderer.render({
          kernel: c.kernel,
          variant,
          options,
          beat: c.beat,
          depth: c.depth ?? SPIRAL_DEPTH,
          input: dry,
          sampleRate: sr,
          bpm,
          seed,
          sendOnSample,
          sendOffSample,
        });
        if (!Array.isArray(out) || out.length !== 2 || out[0].length !== n) {
          throw new Error(
            `renderer ${renderer.id} broke the contract for ${key}`,
          );
        }
        _checkFiniteAndPeak(out, {
          file: `clips/${c.id}-raw.wav`,
          kernel: c.kernel,
          variant,
        });
        cache.set(key, out);
      }
      rendered[variant] = cache.get(key);
    }
    const matched = levelMatchPair(rendered[aVar], rendered[bVar], sr);
    const aPath = `clips/${c.id}-a.wav`;
    const bPath = `clips/${c.id}-b.wav`;
    _checkFiniteAndPeak(matched.a, {
      file: aPath,
      kernel: c.kernel,
      variant: aVar,
    });
    _checkFiniteAndPeak(matched.b, {
      file: bPath,
      kernel: c.kernel,
      variant: bVar,
    });
    clips.push([aPath, matched.a], [bPath, matched.b]);
    pairs.push({
      id: c.id,
      a: aPath,
      b: bPath,
      question: QUESTION,
      meta: {
        key: c.key,
        kernel: c.kernel,
        axis: c.axis,
        beat: c.beat ?? null,
        depth: c.depth ?? null,
        fixture: fixture.name,
        send_on_beat: SEND_ON_BEAT,
        send_off_beat: SEND_OFF_BEAT,
        a_variant: aVar,
        b_variant: bVar,
        options: {
          a: VARIANTS[c.kernel][aVar],
          b: VARIANTS[c.kernel][bVar],
        },
        lufs_a: matched.lufs_a,
        lufs_b: matched.lufs_b,
        lufs_delta_db: matched.lufs_delta_db,
        match_gain_db: matched.match_gain_db,
      },
    });
    blindKey.pairs.push({
      id: c.id,
      a_variant: aVar,
      b_variant: bVar,
      a: aPath,
      b: bPath,
    });
  }

  const listening_seconds = listeningSeconds(pairs.length, bpm, CLIP_BARS);
  if (listening_seconds > LISTENING_MAX_SEC) {
    throw new Error(
      `listening_seconds ${listening_seconds} exceeds ${LISTENING_MAX_SEC} s budget for ${pairs.length} pairs`,
    );
  }

  const manifest = {
    family: FAMILY,
    round: 1,
    seed,
    generated_by: GENERATED_BY,
    renderer: { id: renderer.id, real_path: renderer.realPath },
    sample_rate: sr,
    bpm,
    bars: CLIP_BARS,
    samples: n,
    lufs_match_max_db: LUFS_MATCH_MAX_DB,
    listening_seconds,
    fixtures: [
      {
        name: fixture.name,
        source: fixture.source,
        dry_rms_dbfs: fixture.dry_rms_dbfs,
        peak_dbfs: fixture.peak_dbfs,
      },
    ],
    variants: VARIANTS,
    vetoes_applied: ["finite", "peak"],
    vetoes_not_applied: VETOES_NOT_APPLIED,
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
  const opts = { fixtures: {} };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const val = argv[++i];
    if (val === undefined) throw new Error(`${flag} needs a value`);
    if (flag === "--out") opts.outDir = val;
    else if (flag === "--seed") opts.seed = Number(val);
    else if (flag === "--sample-rate") opts.sampleRate = Number(val);
    else if (flag === "--fixture") {
      const eq = val.indexOf("=");
      if (eq <= 0)
        throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
      const name = val.slice(0, eq);
      if (name !== FIXTURE_NAME)
        throw new Error(`unknown fixture ${name}; only ${FIXTURE_NAME}`);
      opts.fixtures[name] = val.slice(eq + 1);
    } else throw new Error(`unknown argument ${flag}`);
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

export async function main(argv) {
  let opts;
  try {
    opts = _parseArgs(argv);
  } catch (err) {
    console.error(`[ERROR] ${err.message}`);
    return 1;
  }
  let renderer;
  try {
    renderer = resolveRealRenderer();
  } catch (err) {
    if (!(err instanceof UnavailableError)) throw err;
    console.error(`[UNAVAILABLE] ${err.message}`);
    return EXIT_UNAVAILABLE;
  }
  try {
    const m = await exportTimeFxRatingPairs({ ...opts, renderer });
    console.log(
      `[OK] wrote ${m.pairs.length} pairs, ${m.pairs.length * 2} clips, listening ${m.listening_seconds} s, manifest ${join(opts.outDir, "pairs.json")}, seed ${m.seed}`,
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
