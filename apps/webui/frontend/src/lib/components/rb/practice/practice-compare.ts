/**
 * Practice blind A/B compare: same mm-per-px for replica and reference (ADR-0137, issue #9406).
 * Pure utilities importable from Node (--experimental-strip-types) and the compare route.
 */

/** Documented FLX4 panel width (mm); runtime uses layout.panel.w. */
export const PANEL_REFERENCE_WIDTH_MM = 482.6;

export function pxPerMm(imageWidthPx: number, panelWidthMm: number): number {
	if (imageWidthPx <= 0 || panelWidthMm <= 0) {
		throw new Error('pxPerMm requires positive image width and panel width');
	}
	return Math.round((imageWidthPx / panelWidthMm) * 100) / 100;
}

export function layerSize(
	panel: { w: number; d: number },
	scale: number
): { width: number; height: number } {
	return { width: panel.w * scale, height: panel.d * scale };
}

export function compareScale(
	panel: { w: number; d: number },
	image: { width: number; height: number } | null,
	override?: number | null
): {
	pxPerMm: number;
	replica: { width: number; height: number };
	reference: { width: number; height: number } | null;
} {
	let s: number;
	if (override !== undefined && override !== null && override > 0) {
		s = Math.round(override * 100) / 100;
	} else if (image !== null) {
		s = pxPerMm(image.width, panel.w);
	} else {
		s = 2.0;
	}
	const replica = layerSize(panel, s);
	const reference = image === null ? null : { width: image.width, height: image.height };
	return { pxPerMm: s, replica, reference };
}

export type RefSourceKind = 'url' | 'file' | 'none';

export function refSource(ref: string | null): {
	kind: RefSourceKind;
	url?: string;
	fileName?: string;
} {
	if (ref === null || ref === '') return { kind: 'none' };
	if (ref.startsWith('file://')) {
		try {
			const u = new URL(ref);
			const base = u.pathname.split('/').pop() ?? 'image';
			return { kind: 'file', url: ref, fileName: decodeURIComponent(base) };
		} catch {
			return { kind: 'file', url: ref, fileName: 'image' };
		}
	}
	return { kind: 'url', url: ref };
}

/** Mulberry32 PRNG; seed with 32-bit int. */
export function mulberry32(seed: number): () => number {
	let t = seed >>> 0;
	return () => {
		t += 0x6d2b79f5;
		let x = t;
		x = Math.imul(x ^ (x >>> 15), x | 1);
		x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
		return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
	};
}

export function shuffledOrder<T>(items: readonly T[], random: () => number): T[] {
	const out = [...items];
	for (let i = out.length - 1; i > 0; i--) {
		const j = Math.floor(random() * (i + 1));
		[out[i], out[j]] = [out[j]!, out[i]!];
	}
	return out;
}

export type CompareSource = 'replica' | 'reference';

export type CompareTrial = {
	trial_id: string;
	region: { x: number; y: number; w: number; h: number };
	order: [CompareSource, CompareSource];
	rating: number | null;
	choice: CompareSource | null;
};

export function blindTrials(
	panel: { w: number; d: number },
	seed: number,
	cols = 3,
	rows = 2
): CompareTrial[] {
	const cellW = panel.w / cols;
	const cellH = panel.d / rows;
	const rand = mulberry32(seed);
	const trials: CompareTrial[] = [];
	let n = 0;
	for (let row = 0; row < rows; row++) {
		for (let col = 0; col < cols; col++) {
			n += 1;
			const x = col * cellW;
			const y = row * cellH;
			const order = shuffledOrder<CompareSource>(['replica', 'reference'], rand) as [
				CompareSource,
				CompareSource
			];
			trials.push({
				trial_id: `t${n}`,
				region: { x, y, w: cellW, h: cellH },
				order,
				rating: null,
				choice: null
			});
		}
	}
	return trials;
}

export function recordAnswer(
	trials: readonly CompareTrial[],
	trialId: string,
	letter: 'A' | 'B',
	rating: number
): CompareTrial[] {
	if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
		throw new Error('rating must be an integer from 1 to 5');
	}
	const idx = trials.findIndex((t) => t.trial_id === trialId);
	if (idx === -1) throw new Error(`unknown trial id ${trialId}`);
	const t = trials[idx]!;
	const choice = letter === 'A' ? t.order[0]! : t.order[1]!;
	return trials.map((x, i) => (i === idx ? { ...x, choice, rating } : { ...x }));
}

export function buildCompareReport(args: {
	device: string;
	ref: string;
	seed: number;
	pxPerMm: number;
	trials: readonly CompareTrial[];
	recordedAt: string;
}): {
	format: 'opendj-practice-compare/1';
	device: string;
	ref: string;
	seed: number;
	px_per_mm: number;
	recorded_at: string;
	trials: Array<{
		trial_id: string;
		region: { x: number; y: number; w: number; h: number };
		order: [CompareSource, CompareSource];
		rating: number;
		choice: CompareSource;
	}>;
} {
	const answered = args.trials.filter(
		(t): t is CompareTrial & { rating: number; choice: CompareSource } =>
			t.rating !== null && t.choice !== null
	);
	return {
		format: 'opendj-practice-compare/1',
		device: args.device,
		ref: args.ref,
		seed: args.seed,
		px_per_mm: args.pxPerMm,
		recorded_at: args.recordedAt,
		trials: answered.map((t) => ({
			trial_id: t.trial_id,
			region: t.region,
			order: t.order,
			rating: t.rating,
			choice: t.choice
		}))
	};
}

// ----- typed session, commands and query (issue #9406, agent parity) -----------------

export const COMPARE_MODES = ['split', 'overlay', 'blind'] as const;
export type CompareMode = (typeof COMPARE_MODES)[number];

export type CompareSession = {
	mode: CompareMode;
	opacity_pct: number;
	trials: CompareTrial[];
	index: number;
	last_error: string | null;
};

export type CompareCommand =
	| { type: 'set_mode'; mode: CompareMode }
	| { type: 'set_opacity'; pct: number }
	| { type: 'answer'; letter: 'A' | 'B'; rating: number }
	| { type: 'goto'; index: number };

export function newCompareSession(trials: CompareTrial[]): CompareSession {
	return {
		mode: 'split',
		opacity_pct: 50,
		trials,
		index: 0,
		last_error: null
	};
}

function _isCompareMode(x: unknown): x is CompareMode {
	return typeof x === 'string' && (COMPARE_MODES as readonly string[]).includes(x);
}

export function parseCompareCommand(value: unknown): CompareCommand {
	if (value === null || typeof value !== 'object' || Array.isArray(value)) {
		throw new TypeError('command must be a plain object');
	}
	const o = value as Record<string, unknown>;
	const t = o.type;
	if (t !== 'set_mode' && t !== 'set_opacity' && t !== 'answer' && t !== 'goto') {
		throw new TypeError('command.type must be set_mode, set_opacity, answer or goto');
	}
	if (t === 'set_mode') {
		if (!_isCompareMode(o.mode))
			throw new TypeError('set_mode.mode must be split, overlay or blind');
		return { type: 'set_mode', mode: o.mode };
	}
	if (t === 'set_opacity') {
		if (typeof o.pct !== 'number' || !Number.isFinite(o.pct)) {
			throw new TypeError('set_opacity.pct must be a number');
		}
		return { type: 'set_opacity', pct: o.pct };
	}
	if (t === 'answer') {
		if (o.letter !== 'A' && o.letter !== 'B') {
			throw new TypeError('answer.letter must be A or B');
		}
		if (typeof o.rating !== 'number' || !Number.isFinite(o.rating)) {
			throw new TypeError('answer.rating must be a number');
		}
		return { type: 'answer', letter: o.letter, rating: o.rating };
	}
	if (typeof o.index !== 'number' || !Number.isFinite(o.index)) {
		throw new TypeError('goto.index must be a number');
	}
	return { type: 'goto', index: o.index };
}

export function applyCompareCommand(s: CompareSession, cmd: CompareCommand): CompareSession {
	const base = { ...s, trials: s.trials.map((x) => ({ ...x })), last_error: null as string | null };
	if (cmd.type === 'set_mode') {
		if (!_isCompareMode(cmd.mode)) throw new Error(`unknown compare mode ${String(cmd.mode)}`);
		return { ...base, mode: cmd.mode };
	}
	if (cmd.type === 'set_opacity') {
		const p = cmd.pct;
		if (!Number.isInteger(p) || p < 0 || p > 100) {
			throw new Error('opacity must be an integer from 0 to 100');
		}
		return { ...base, opacity_pct: p };
	}
	if (cmd.type === 'goto') {
		const i = cmd.index;
		if (!Number.isInteger(i) || i < 0 || i >= base.trials.length) {
			throw new Error('goto index out of range');
		}
		return { ...base, index: i };
	}
	// answer
	if (base.trials.length === 0) throw new Error('no blind trials');
	const cur = base.trials[base.index];
	if (cur === undefined) throw new Error('trial index out of range');
	const nextTrials = recordAnswer(base.trials, cur.trial_id, cmd.letter, cmd.rating);
	let nextIndex = base.index;
	if (nextIndex + 1 < nextTrials.length) nextIndex += 1;
	return { ...base, trials: nextTrials, index: nextIndex };
}

export type CompareQuery = {
	version: 1;
	device: string;
	seed: number;
	ref: string;
	px_per_mm: number | null;
	mode: CompareMode;
	opacity_pct: number;
	index: number;
	trial_count: number;
	answered: number;
	current_trial_id: string | null;
	answers: Array<{ trial_id: string; rating: number; choice: CompareSource }>;
	last_error: string | null;
};

export function compareQuery(
	s: CompareSession,
	ctx: { device: string; seed: number; ref: string; pxPerMm: number | null }
): CompareQuery {
	const answeredList = s.trials.filter(
		(t): t is CompareTrial & { rating: number; choice: CompareSource } =>
			t.rating !== null && t.choice !== null
	);
	return {
		version: 1,
		device: ctx.device,
		seed: ctx.seed,
		ref: ctx.ref,
		px_per_mm: ctx.pxPerMm,
		mode: s.mode,
		opacity_pct: s.opacity_pct,
		index: s.index,
		trial_count: s.trials.length,
		answered: answeredList.length,
		current_trial_id: s.trials[s.index]?.trial_id ?? null,
		answers: answeredList.map((t) => ({
			trial_id: t.trial_id,
			rating: t.rating!,
			choice: t.choice!
		})),
		last_error: s.last_error
	};
}
