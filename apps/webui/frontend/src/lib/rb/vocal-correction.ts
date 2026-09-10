/**
 * Vocal-area correction pins (FB-12 / issue #698).
 *
 * Right-click on a loaded wavestack row or deck strip maps pointer X onto
 * track time, names the painted region when the click falls inside one, and
 * encodes that into an FB-03 `vocal-area:` anchor. Regions are never edited
 * here; harvest already prints `near {anchor}`.
 *
 * DOM-free so node:test can drive it with real-shaped vocals objects.
 */

import { WAVE_WINDOW_S } from "$lib/components/rb/wave/render";
import { waveClickTargetMs } from "$lib/components/rb/wave/wave-scrub";

export type VocalStatus =
  | "rekordbox"
  | "no_vocals"
  | "demucs"
  | "not_analyzed";

export type VocalRegionLike = {
  start_s: number;
  end_s: number;
  intensity: number;
};

/** Structural twin of api-rb `Vocals` so this module does not import api-rb. */
export type VocalsLike =
  | { status: "rekordbox"; fps: number; regions: readonly VocalRegionLike[] }
  | { status: "no_vocals"; fps: number; regions: readonly VocalRegionLike[] }
  | { status: "demucs"; fps: number; regions: readonly VocalRegionLike[] }
  | { status: "not_analyzed" };

export type VocalCorrectionHit = {
  stable_id: string;
  status: VocalStatus;
} & (
  | { kind: "region"; start_s: number; end_s: number; intensity: number }
  | { kind: "at"; time_s: number }
);

const VOCAL_STATUSES: readonly VocalStatus[] = [
  "rekordbox",
  "no_vocals",
  "demucs",
  "not_analyzed",
];

const REGION_ANCHOR =
  /^vocal-area:([^:@]+):(\d+\.\d{2})-(\d+\.\d{2}):(rekordbox|no_vocals|demucs|not_analyzed)$/;
const AT_ANCHOR =
  /^vocal-area:([^:@]+)@(\d+\.\d{2}):(rekordbox|no_vocals|demucs|not_analyzed)$/;

function formatSeconds(value: number): string {
  return value.toFixed(2);
}

function isVocalStatus(value: string): value is VocalStatus {
  return (VOCAL_STATUSES as readonly string[]).includes(value);
}

/** First region where `start_s <= timeS < end_s`. Null when none. */
export function regionAtTime(
  regions: readonly VocalRegionLike[],
  timeS: number,
): VocalRegionLike | null {
  for (const region of regions) {
    if (region.start_s <= timeS && timeS < region.end_s) return region;
  }
  return null;
}

export function hitFromVocals(args: {
  stableId: string;
  vocals: VocalsLike;
  timeS: number;
}): VocalCorrectionHit {
  const { stableId, vocals, timeS } = args;
  if (vocals.status === "rekordbox" || vocals.status === "demucs") {
    const region = regionAtTime(vocals.regions, timeS);
    if (region !== null) {
      return {
        stable_id: stableId,
        status: vocals.status,
        kind: "region",
        start_s: region.start_s,
        end_s: region.end_s,
        intensity: region.intensity,
      };
    }
  }
  return {
    stable_id: stableId,
    status: vocals.status,
    kind: "at",
    time_s: timeS,
  };
}

export function waveRowTimeS(args: {
  pointerX: number;
  widthPx: number;
  centerPositionMs: number;
  durationMs: number;
  pitch: number;
}): number {
  return (
    waveClickTargetMs({
      centerPositionMs: args.centerPositionMs,
      durationMs: args.durationMs,
      pointerX: args.pointerX,
      widthPx: args.widthPx,
      windowSeconds: WAVE_WINDOW_S * args.pitch,
    }) / 1000
  );
}

export function stripTimeS(args: {
  pointerX: number;
  widthPx: number;
  durationMs: number;
}): number {
  const { pointerX, widthPx, durationMs } = args;
  const ms = Math.min(
    durationMs,
    Math.max(0, (pointerX / widthPx) * durationMs),
  );
  return ms / 1000;
}

export function encodeVocalAnchor(hit: VocalCorrectionHit): string {
  if (hit.kind === "region") {
    return (
      `vocal-area:${hit.stable_id}:` +
      `${formatSeconds(hit.start_s)}-${formatSeconds(hit.end_s)}:${hit.status}`
    );
  }
  return `vocal-area:${hit.stable_id}@${formatSeconds(hit.time_s)}:${hit.status}`;
}

export function parseVocalAnchor(anchor: string): VocalCorrectionHit | null {
  const region = REGION_ANCHOR.exec(anchor);
  if (region !== null && isVocalStatus(region[4])) {
    return {
      stable_id: region[1],
      status: region[4],
      kind: "region",
      start_s: Number(region[2]),
      end_s: Number(region[3]),
      intensity: 0,
    };
  }
  const at = AT_ANCHOR.exec(anchor);
  if (at !== null && isVocalStatus(at[3])) {
    return {
      stable_id: at[1],
      status: at[3],
      kind: "at",
      time_s: Number(at[2]),
    };
  }
  return null;
}

export function formatVocalComment(hit: VocalCorrectionHit): string {
  if (hit.kind === "region") {
    return (
      `Incorrect vocal area ${formatSeconds(hit.start_s)}s-` +
      `${formatSeconds(hit.end_s)}s (${hit.status}) on ${hit.stable_id}`
    );
  }
  return (
    `Incorrect vocal area at ${formatSeconds(hit.time_s)}s (${hit.status}) ` +
    `on ${hit.stable_id}`
  );
}
