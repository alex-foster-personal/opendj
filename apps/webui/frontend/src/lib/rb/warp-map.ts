/**
 * Variable-tempo warp map: source-audio time to musical beat coordinates.
 *
 * Issue #11199 / parent #425. Schema and fail-closed validation only; no UI,
 * no Signalsmith schedule yet. A saved document is nondestructive: it never
 * changes source audio, only maps it.
 *
 * Requirements (WARP-*):
 *   ✔︎ ✅ 🎯 PQTZ with nonuniform intervals maps beats and round-trips.
 *     [if] four or more nonuniform source intervals from real PQTZ [then ⛔️]
 *   ✔︎ ✅ 🎯 Invalid anchors fail before any processor schedule is committed.
 *     [if] duplicate, non-finite or non-monotonic coordinates [then ⛔️]
 *   ✔︎ ✅ 🎯 Absent or invalid analysis never synthesizes a fallback grid.
 *     [if] analysis is missing or fails PQTZ validation [then ⛔️]
 *   ✔︎ ✅ 🎯 analyzed, locked and creative kinds persist through save/load.
 *     [if] mixed kinds are serialized and parsed [then ⛔️]
 */
import type { AnlzBeat, AnlzBeatgrid } from '$lib/rb/anlz-types';
import { requireBeatGridForWarpMap } from '$lib/rb/beat-sync-math';

// ------------------------------------------------------------------------- schema

export const WARP_MAP_SCHEMA_VERSION = 1 as const;
export const WARP_MAP_MIN_ANCHORS = 2;

/** How an anchor was created or confirmed. */
export type WarpAnchorKind = 'analyzed' | 'locked' | 'creative';

/** One map knot: source time and its musical beat coordinate. */
export interface WarpAnchor {
	/** Source audio time in seconds from track start. */
	source_s: number;
	/** Musical beat coordinate (0 = first beat, 1 = second, ...). */
	beat: number;
	kind: WarpAnchorKind;
}

export interface WarpMapDocument {
	v: typeof WARP_MAP_SCHEMA_VERSION;
	anchors: WarpAnchor[];
	/** SHA-256 of the canonical serialized document (checksum-ready fixtures). */
	checksum?: string;
}

/** Serializable interval between two adjacent anchors. */
export interface WarpMapInterval {
	source_s0: number;
	source_s1: number;
	beat0: number;
	beat1: number;
}

export const WARP_MAP_FIXTURE_MANIFEST_SCHEMA = 1;

const _ALLOWED_KINDS: ReadonlySet<WarpAnchorKind> = new Set(['analyzed', 'locked', 'creative']);

// ------------------------------------------------------------------------- errors

export class WarpMapValidationError extends Error {
	readonly code = 'WARP_MAP_INVALID';

	constructor(message: string) {
		super(message);
		this.name = 'WarpMapValidationError';
	}
}

/** Gate before a processor schedule would be built from a map (fail closed). */
export function assertWarpMapSchedulable(doc: WarpMapDocument): void {
	validateWarpMap(doc);
}

// ------------------------------------------------------------------------- build

/**
 * Build analyzed anchors from a validated PQTZ grid. Fails when analysis is
 * absent or invalid; never synthesizes a fallback beat grid.
 */
export function buildWarpMapFromPqtz(beats: readonly AnlzBeat[]): WarpMapDocument {
	requireBeatGridForWarpMap(beats);
	const anchors: WarpAnchor[] = beats.map((beat, index) => ({
		source_s: beat.t,
		beat: index,
		kind: 'analyzed'
	}));
	const doc: WarpMapDocument = { v: WARP_MAP_SCHEMA_VERSION, anchors };
	validateWarpMap(doc);
	return doc;
}

/** Same as {@link buildWarpMapFromPqtz} from a served `/anlz` beatgrid block. */
export function buildWarpMapFromBeatgrid(beatgrid: AnlzBeatgrid): WarpMapDocument {
	if (beatgrid.status === 'failed' || beatgrid.status === 'missing') {
		throw new WarpMapValidationError(
			`warp map: beatgrid status is ${beatgrid.status ?? 'unknown'}; no fallback grid synthesized`
		);
	}
	if (!Array.isArray(beatgrid.beats) || beatgrid.beats.length < WARP_MAP_MIN_ANCHORS) {
		throw new WarpMapValidationError(
			`warp map: analysis absent (${beatgrid.beats?.length ?? 0} beats); no fallback grid synthesized`
		);
	}
	if (beatgrid.fallback === true) {
		throw new WarpMapValidationError(
			'warp map: beatgrid-fallback grids cannot seed a warp map; no fallback grid synthesized'
		);
	}
	return buildWarpMapFromPqtz(beatgrid.beats);
}

// ------------------------------------------------------------------------- validate

export function validateWarpMap(doc: WarpMapDocument): void {
	if (doc.v !== WARP_MAP_SCHEMA_VERSION) {
		throw new WarpMapValidationError(`warp map: unsupported schema v=${String(doc.v)}`);
	}
	if (!Array.isArray(doc.anchors)) {
		throw new WarpMapValidationError('warp map: anchors must be an array');
	}
	if (doc.anchors.length < WARP_MAP_MIN_ANCHORS) {
		throw new WarpMapValidationError(
			`warp map: need at least ${WARP_MAP_MIN_ANCHORS} anchors, got ${doc.anchors.length}`
		);
	}
	let prevSource = -Infinity;
	let prevBeat = -Infinity;
	for (let index = 0; index < doc.anchors.length; index++) {
		const anchor = doc.anchors[index];
		_assertAnchor(index, anchor);
		if (anchor.source_s <= prevSource) {
			throw new WarpMapValidationError(
				`warp map: source_s must be strictly increasing at anchor[${index}]`
			);
		}
		if (anchor.beat <= prevBeat) {
			throw new WarpMapValidationError(
				`warp map: beat must be strictly increasing at anchor[${index}]`
			);
		}
		prevSource = anchor.source_s;
		prevBeat = anchor.beat;
	}
}

function _assertAnchor(index: number, anchor: unknown): asserts anchor is WarpAnchor {
	if (typeof anchor !== 'object' || anchor === null) {
		throw new WarpMapValidationError(`warp map: anchor[${index}] must be an object`);
	}
	const a = anchor as WarpAnchor;
	if (!Number.isFinite(a.source_s) || a.source_s < 0) {
		throw new WarpMapValidationError(`warp map: anchor[${index}].source_s must be finite and >= 0`);
	}
	if (!Number.isFinite(a.beat) || a.beat < 0) {
		throw new WarpMapValidationError(`warp map: anchor[${index}].beat must be finite and >= 0`);
	}
	if (!_ALLOWED_KINDS.has(a.kind)) {
		throw new WarpMapValidationError(`warp map: anchor[${index}].kind is invalid`);
	}
}

// ------------------------------------------------------------------------- serialize

/** Stable JSON object for persistence and checksums (sorted keys, no checksum field). */
export function warpMapToJsonObject(doc: WarpMapDocument): Record<string, unknown> {
	return {
		v: WARP_MAP_SCHEMA_VERSION,
		anchors: doc.anchors.map((anchor) => ({
			source_s: roundSourceS(anchor.source_s),
			beat: roundBeat(anchor.beat),
			kind: anchor.kind
		}))
	};
}

export function serializeWarpMap(doc: WarpMapDocument): string {
	return `${JSON.stringify(warpMapToJsonObject(doc))}\n`;
}

export function parseWarpMap(raw: string | unknown): WarpMapDocument {
	const value = typeof raw === 'string' ? JSON.parse(raw) : raw;
	if (typeof value !== 'object' || value === null) {
		throw new WarpMapValidationError('warp map: document must be an object');
	}
	const record = value as Record<string, unknown>;
	if (record.v !== WARP_MAP_SCHEMA_VERSION) {
		throw new WarpMapValidationError(`warp map: unsupported schema v=${String(record.v)}`);
	}
	if (!Array.isArray(record.anchors)) {
		throw new WarpMapValidationError('warp map: anchors must be an array');
	}
	const anchors: WarpAnchor[] = record.anchors.map((item, index) => {
		if (typeof item !== 'object' || item === null) {
			throw new WarpMapValidationError(`warp map: anchor[${index}] must be an object`);
		}
		const a = item as Record<string, unknown>;
		const kind = a.kind;
		if (kind !== 'analyzed' && kind !== 'locked' && kind !== 'creative') {
			throw new WarpMapValidationError(`warp map: anchor[${index}].kind is invalid`);
		}
		return {
			source_s: Number(a.source_s),
			beat: Number(a.beat),
			kind
		};
	});
	const doc: WarpMapDocument = { v: WARP_MAP_SCHEMA_VERSION, anchors };
	validateWarpMap(doc);
	const checksum = typeof record.checksum === 'string' ? record.checksum : undefined;
	return checksum === undefined ? doc : { ...doc, checksum };
}

export function roundSourceS(seconds: number): number {
	return Math.round(seconds * 1000) / 1000 + 0;
}

export function roundBeat(beat: number): number {
	return Math.round(beat * 1000) / 1000 + 0;
}

/** Canonical UTF-8 input for fixture SHA-256 (computed in tests or offline). */
export function warpMapChecksumInput(doc: WarpMapDocument): string {
	return serializeWarpMap(doc);
}

export function withWarpMapChecksum(doc: WarpMapDocument, checksum: string): WarpMapDocument {
	return { ...doc, checksum };
}

/** Adjacent source/beat spans; length is anchors.length - 1. */
export function warpMapIntervals(doc: WarpMapDocument): WarpMapInterval[] {
	assertWarpMapSchedulable(doc);
	const out: WarpMapInterval[] = [];
	for (let index = 1; index < doc.anchors.length; index++) {
		const a0 = doc.anchors[index - 1];
		const a1 = doc.anchors[index];
		out.push({
			source_s0: a0.source_s,
			source_s1: a1.source_s,
			beat0: a0.beat,
			beat1: a1.beat
		});
	}
	return out;
}
