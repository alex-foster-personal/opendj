/**
 * Pure frame-to-draw-command mapping for the 2D lighting stage. No module
 * imports: types and math only, so node tests can load it without a DOM.
 */

export type Rgb = readonly [number, number, number];

export interface FixtureState {
	id: string;
	rgb: Rgb;
	intensity: number;
	pan_deg: number | null;
	tilt_deg: number | null;
	strobe_hz: number;
}

export interface FrameMessage {
	t_ns: number;
	patch_version: string | number | null;
	fixtures: FixtureState[];
}

export interface StageFixture {
	id: string;
	x: number;
	y: number;
	groups: string[];
	pan_range_deg?: number;
	tilt_range_deg?: number;
}

export interface StagePatch {
	version: string | number | null;
	fixtures: StageFixture[];
}

export interface StageSize {
	width: number;
	height: number;
	margin?: number;
}

export type DrawCommand =
	| { kind: 'clear'; width: number; height: number; color: string }
	| {
			kind: 'beam';
			id: string;
			x1: number;
			y1: number;
			x2: number;
			y2: number;
			color: string;
			lineWidth: number;
	  }
	| { kind: 'disc'; id: string; x: number; y: number; r: number; color: string; stroke: string }
	| { kind: 'flash'; id: string; x: number; y: number; r: number; color: string }
	| { kind: 'label'; id: string; x: number; y: number; text: string; color: string };

export const STAGE_BACKGROUND = 'rgb(10, 10, 14)';
export const STAGE_MARGIN_PX = 32;
export const DEFAULT_PAN_RANGE_DEG = 540;
export const DEFAULT_TILT_RANGE_DEG = 270;
export const STROBE_DUTY = 0.25;
export const UNLIT_STROKE = 'rgb(70, 70, 80)';
export const FLASH_COLOR = 'rgb(255, 255, 255)';

const LABEL_COLOR = 'rgb(150, 150, 160)';

function clamp01(v: number): number {
	if (v < 0) return 0;
	if (v > 1) return 1;
	return v;
}

function clampChannel(v: number): number {
	if (v < 0) return 0;
	if (v > 255) return 255;
	return v;
}

function frac(t: number): number {
	const f = t - Math.floor(t);
	return f < 0 ? f + 1 : f;
}

export function cssRgb(rgb: Rgb, intensity: number): string {
	const k = clamp01(intensity);
	const r = Math.round(clampChannel(rgb[0]) * k);
	const g = Math.round(clampChannel(rgb[1]) * k);
	const b = Math.round(clampChannel(rgb[2]) * k);
	return `rgb(${r}, ${g}, ${b})`;
}

export function stageToCanvas(x: number, y: number, size: StageSize): { x: number; y: number } {
	const margin = size.margin ?? STAGE_MARGIN_PX;
	const innerW = size.width - 2 * margin;
	const innerH = size.height - 2 * margin;
	return {
		x: margin + clamp01(x) * innerW,
		y: margin + clamp01(y) * innerH
	};
}

/**
 * Pan maps as a dial over the fixture's full pan range (default 540 deg):
 * theta = (pan / range) * 2pi, beam (dx, dy) = (sin(theta), -cos(theta)).
 * Pan 0 points upstage (top of canvas); pan at half the range points downstage,
 * opposite to 0, so start, centre and end of travel read clearly on screen.
 * Reach scales beam length from tilt only.
 */
export function beamVector(
	panDeg: number,
	tiltDeg: number,
	panRangeDeg: number = DEFAULT_PAN_RANGE_DEG,
	tiltRangeDeg: number = DEFAULT_TILT_RANGE_DEG
): { dx: number; dy: number; reach: number } {
	const panRange = panRangeDeg > 0 ? panRangeDeg : DEFAULT_PAN_RANGE_DEG;
	const tiltRange = tiltRangeDeg > 0 ? tiltRangeDeg : DEFAULT_TILT_RANGE_DEG;
	let pan = panDeg;
	if (pan < 0) pan = 0;
	if (pan > panRange) pan = panRange;
	const theta = (pan / panRange) * (2 * Math.PI);
	const dx = Math.sin(theta);
	const dy = -Math.cos(theta);
	const reach = clamp01(tiltDeg / tiltRange);
	return { dx, dy, reach };
}

export function layoutFallback(ids: string[]): StageFixture[] {
	const n = ids.length;
	if (n === 0) return [];
	return ids.map((id, i) => ({
		id,
		x: (i + 1) / (n + 1),
		y: 0.5,
		groups: []
	}));
}

const UNLIT_STATE: FixtureState = {
	id: '',
	rgb: [0, 0, 0],
	intensity: 0,
	pan_deg: null,
	tilt_deg: null,
	strobe_hz: 0
};

export function renderStage(
	frame: FrameMessage | null,
	patch: StagePatch | null,
	size: StageSize,
	opts?: { timeS?: number }
): DrawCommand[] {
	const w = size.width;
	const h = size.height;
	const r = Math.max(6, Math.min(w, h) * 0.035);
	const reachScale = Math.min(w, h) * 0.25;

	const frameById = new Map<string, FixtureState>();
	if (frame !== null) {
		for (const fx of frame.fixtures) frameById.set(fx.id, fx);
	}

	const patchById = new Map<string, StageFixture>();
	if (patch !== null) {
		for (const fx of patch.fixtures) patchById.set(fx.id, fx);
	}

	const allIds = new Set<string>();
	for (const id of patchById.keys()) allIds.add(id);
	for (const id of frameById.keys()) allIds.add(id);
	const sortedIds = [...allIds].sort();

	const unplacedIds = sortedIds.filter((id) => !patchById.has(id));
	const fallbackList = layoutFallback(unplacedIds);
	const fallbackById = new Map(fallbackList.map((fx) => [fx.id, fx]));

	const placed: Array<{ stage: StageFixture; state: FixtureState }> = [];
	for (const id of sortedIds) {
		const stage = patchById.get(id) ?? fallbackById.get(id);
		if (stage === undefined) continue;
		const state = frameById.get(id) ?? { ...UNLIT_STATE, id };
		placed.push({ stage, state: { ...state, id } });
	}

	const beams: DrawCommand[] = [];
	const discs: DrawCommand[] = [];
	const flashes: DrawCommand[] = [];
	const labels: DrawCommand[] = [];

	const timeS = opts?.timeS ?? (frame !== null ? frame.t_ns / 1e9 : 0);

	for (const { stage, state } of placed) {
		const centre = stageToCanvas(stage.x, stage.y, size);
		const discColor =
			state.intensity > 0 || frameById.has(state.id)
				? cssRgb(state.rgb, state.intensity)
				: 'rgb(0, 0, 0)';

		const panRange = stage.pan_range_deg ?? DEFAULT_PAN_RANGE_DEG;
		const tiltRange = stage.tilt_range_deg ?? DEFAULT_TILT_RANGE_DEG;

		if (typeof state.pan_deg === 'number' && Number.isFinite(state.pan_deg)) {
			const tilt = state.tilt_deg ?? 0;
			const { dx, dy, reach } = beamVector(state.pan_deg, tilt, panRange, tiltRange);
			const beamLen = r * 1.5 + reach * reachScale;
			beams.push({
				kind: 'beam',
				id: state.id,
				x1: centre.x,
				y1: centre.y,
				x2: centre.x + dx * beamLen,
				y2: centre.y + dy * beamLen,
				color: discColor,
				lineWidth: Math.max(2, r * 0.3)
			});
		}

		discs.push({
			kind: 'disc',
			id: state.id,
			x: centre.x,
			y: centre.y,
			r,
			color: discColor,
			stroke: UNLIT_STROKE
		});

		if (state.strobe_hz > 0 && state.intensity > 0) {
			const phase = frac(timeS * state.strobe_hz);
			if (phase < STROBE_DUTY) {
				flashes.push({
					kind: 'flash',
					id: state.id,
					x: centre.x,
					y: centre.y,
					r: r * 1.3,
					color: FLASH_COLOR
				});
			}
		}

		labels.push({
			kind: 'label',
			id: state.id,
			x: centre.x,
			y: centre.y + r + 12,
			text: state.id,
			color: LABEL_COLOR
		});
	}

	beams.sort((a, b) => (a.kind === 'beam' && b.kind === 'beam' ? a.id.localeCompare(b.id) : 0));
	discs.sort((a, b) => (a.kind === 'disc' && b.kind === 'disc' ? a.id.localeCompare(b.id) : 0));
	flashes.sort((a, b) => (a.kind === 'flash' && b.kind === 'flash' ? a.id.localeCompare(b.id) : 0));
	labels.sort((a, b) => (a.kind === 'label' && b.kind === 'label' ? a.id.localeCompare(b.id) : 0));

	return [
		{ kind: 'clear', width: w, height: h, color: STAGE_BACKGROUND },
		...beams,
		...discs,
		...flashes,
		...labels
	];
}

export function stageSignature(commands: DrawCommand[]): string {
	const parts: string[] = [];
	for (const cmd of commands) {
		if (cmd.kind === 'disc') parts.push(`${cmd.id}=${cmd.color}`);
		if (cmd.kind === 'flash') parts.push(`${cmd.id}!`);
	}
	return parts.join(',');
}
