/**
 * Edge wave geometry and arming for the edge sampler ((private path) 7.1, 7.4).
 * Pure: no DOM, no clock reads except through arguments.
 */

export const REST_W = 3;
export const AWAKE_W = 32;
/** Top tab width when the edge is awake (mock `HEAD_OPEN_W`). */
export const HEAD_OPEN_W = 476;
/** Top tab band height; tabs start 4 px below it (mock `HEAD_H`). */
export const HEAD_H = 132;
/** Empty slots grow along the same Gaussian, capped here (mock `tabTarget`). */
export const EMPTY_POP_W = 300;
/** A tab click counts only from this width (EDGESAMP-11872g). */
export const CLICK_MIN_W = 150;
/** ...and only this long after the edge woke (EDGESAMP-11872g). */
export const CLICK_SETTLE_MS = 150;
/** Flip stagger: each tab starts this much after the one above it (mock `d._i * 18`). */
export const FLIP_STAGGER_MS = 18;
/** Flip stagger: each tab ramps to its width over this long (mock `/ 140`). */
export const FLIP_RAMP_MS = 140;
export const ZONE_PX = 14;
/** Extra px beyond a tab's popped width that keeps the wave open. */
export const STAY_PAD_PX = 16;
export const LEAVE_MS = 250;
export const OPEN_DELAY_MS = 60;
/** The wheel over an awake edge flips at most one set per this long (mock `260`). */
export const WHEEL_FLIP_MS = 260;
export const EASE_MS = 90;

export interface WaveParams {
	/** tabs: Gaussian width in tab heights (the maintainer's calibrated 2.2) */
	spread: number;
	/** px: fully popped tab width, wide enough to read the whole tab */
	pop: number;
	speed: number;
	delay: number;
	/** tabs per set on each edge (the maintainer's calibrated 20) */
	perSet: number;
}

export const DEFAULT_WAVE: WaveParams = {
	spread: 2.2,
	pop: 476,
	speed: EASE_MS,
	delay: OPEN_DELAY_MS,
	perSet: 20
};

export interface TabWidthInput {
	awake: boolean;
	/** pointer y minus tab centre y, px */
	dy: number;
	tabH: number;
	/** pinned, armed, playing, previewing, or being edited */
	held: boolean;
	/** slot has a sample (empty slots rest hidden until awake) */
	filled: boolean;
	/** 0..1 flip stagger progress (1 = settled), see flipRamp */
	ramp: number;
	/** this edge's search box has focus: every tab pops (issue #11102); omitted = false */
	searchOpen?: boolean;
	/** false when a query or filter hides this slot: it keeps its numbered strip; omitted = true */
	matches?: boolean;
	/** another tab of this edge is in the edit modal: this one settles to the strip; omitted = false */
	editingOther?: boolean;
}

export function tabTargetWidth(t: TabWidthInput, p: WaveParams): number {
	if (!t.filled && !t.awake) return 0;
	const base = t.awake ? AWAKE_W : REST_W;
	if (t.matches === false) return base;
	if (t.held && t.filled) return p.pop;
	if (t.editingOther) return base;
	if (t.searchOpen) return t.filled ? p.pop : EMPTY_POP_W;
	if (!t.awake) return base;
	const peak = t.filled ? p.pop : EMPTY_POP_W;
	const k = t.dy / (p.spread * t.tabH);
	return base + (peak - base) * Math.exp(-k * k) * t.ramp;
}

/** Flip stagger progress of tab `index`: new sets pop top to bottom (mock 18 ms per tab, 140 ms ramp). */
export function flipRamp(t: number, flipT: number, index: number): number {
	return clamp01((t - flipT - index * FLIP_STAGGER_MS) / FLIP_RAMP_MS);
}

/** Opacity of a tab's controls and empty label: visible from about 70 px (mock `--reveal`). */
export const controlsReveal = (w: number): number => clamp01((w - 70) / 90);
/** Opacity of title and pencil: only near full width, so no clipped title (mock `--treveal`). */
export const titleReveal = (w: number): number => clamp01((w - 400) / 50);

/** A click on a tab counts only once it is CLICK_MIN_W wide and CLICK_SETTLE_MS after wake. */
export function clickAllowed(widthPx: number, msSinceWake: number): boolean {
	return widthPx >= CLICK_MIN_W && msSinceWake >= CLICK_SETTLE_MS;
}

/** `S<set>-<nn>` reference of a slot, both 1-based (spec S1-05). */
export function tabRef(setIndex: number, slotIndex: number): string {
	return `S${setIndex + 1}-${String(slotIndex + 1).padStart(2, '0')}`;
}

/** Sets to flip for a wheel tick at `t` (+1 down, -1 up), or 0 when asleep or inside WHEEL_FLIP_MS of the last flip at `lastFlipT`. */
export function wheelFlip(awake: boolean, deltaY: number, t: number, lastFlipT: number): number {
	if (!awake || deltaY === 0 || t - lastFlipT < WHEEL_FLIP_MS) return 0;
	return deltaY > 0 ? 1 : -1;
}

function clamp01(v: number): number {
	return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** One frame of exponential easing toward target; snaps when within 0.3 px or when reduceMotion. */
export function easeWidth(
	current: number,
	target: number,
	dtMs: number,
	p: WaveParams,
	reduceMotion: boolean
): number {
	if (reduceMotion) return target;
	const k = Math.min(1, dtMs / Math.max(10, p.speed / 3));
	const next = current + (target - current) * k;
	return Math.abs(target - next) < 0.3 ? target : next;
}

export interface WakeInput {
	enabled: boolean;
	fromEdgePx: number;
	buttonsHeld: boolean;
	/** loop drag that began inside the app */
	appDrag: boolean;
	/** false after a press or edge-origin drag until the pointer leaves the zone */
	armed: boolean;
	dwellMs: number;
}

export function shouldWake(w: WakeInput, p: WaveParams): boolean {
	if (!w.enabled || w.fromEdgePx > ZONE_PX) return false;
	if (!w.armed && !w.appDrag) return false;
	if (!w.appDrag && w.buttonsHeld) return false;
	return w.dwellMs >= p.delay;
}

/** Pointer arming (7.1): edge-origin entry with a button disarms until leave. */
export function updateArming(
	armed: { value: boolean },
	prevFromEdgePx: number,
	fromEdgePx: number,
	buttons: number,
	appDrag: boolean
): void {
	if (fromEdgePx <= ZONE_PX && prevFromEdgePx > ZONE_PX && buttons !== 0 && !appDrag) {
		armed.value = false;
	}
	if (fromEdgePx > ZONE_PX && buttons === 0) {
		armed.value = true;
	}
}

export function tabIndexAtY(
	pointerY: number,
	top: number,
	headH: number,
	tabH: number,
	tabCount: number
): number | null {
	const y = pointerY - top - headH - 4;
	if (y < 0) return null;
	const i = Math.floor(y / tabH);
	return i >= 0 && i < tabCount ? i : null;
}

export function stayReachPx(
	pointerY: number,
	top: number,
	headH: number,
	tabH: number,
	headW: number,
	widths: readonly number[],
	tabCount: number
): number {
	const y = pointerY - top;
	if (y < headH) return headW + STAY_PAD_PX;
	const i = tabIndexAtY(pointerY, top, headH, tabH, tabCount);
	if (i === null) return REST_W + STAY_PAD_PX;
	return (widths[i] ?? REST_W) + STAY_PAD_PX;
}

export interface EdgeMotionInput {
	enabled: boolean;
	awake: boolean;
	armed: boolean;
	fromEdgePx: number;
	pointerY: number;
	top: number;
	headH: number;
	tabH: number;
	tabCount: number;
	buttons: number;
	inWindow: boolean;
	t: number;
	appDrag: boolean;
	held: readonly boolean[];
	filled: readonly boolean[];
	/** per tab: false when this edge's query or filters hide it; omitted = every tab matches */
	matches?: readonly boolean[];
	/** per tab: another tab of this edge is in the edit modal; omitted = none */
	editingOther?: readonly boolean[];
	/** this edge's search is open: every matching tab pops while the edge is awake */
	searchOpen?: boolean;
	/** keep an awake edge open wherever the pointer is (search box focused); omitted = false */
	hold?: boolean;
	params: WaveParams;
	reduceMotion: boolean;
	dtMs: number;
	headW: number;
	widths: number[];
}

export interface EdgeMotionResult {
	awake: boolean;
	enterT: number;
	leaveT: number;
	activeSince: number;
	/** frame time of the last set flip, drives flipRamp */
	flipT: number;
	headW: number;
	widths: number[];
	needsFrame: boolean;
}

export function createEdgeMotion(tabCount: number): EdgeMotionResult {
	return {
		awake: false,
		enterT: 0,
		leaveT: 0,
		activeSince: -1e9,
		flipT: -1e9,
		headW: REST_W,
		widths: Array.from({ length: tabCount }, () => REST_W),
		needsFrame: false
	};
}

/** Advance one edge for a single frame. Independent per edge (EDGESAMP-11954). */
export function stepEdgeMotion(m: EdgeMotionResult, frame: EdgeMotionInput): void {
	let busy = frame.appDrag;
	const anyHeldVisible = frame.held.some((h, i) => h && frame.filled[i]);
	const fe = frame.fromEdgePx;
	const inZone = fe <= ZONE_PX;

	if (!m.awake) {
		if (frame.enabled && frame.inWindow && frame.pointerY >= frame.top && inZone) {
			if (m.enterT === 0) m.enterT = frame.t;
			const dwellMs = m.enterT > 0 ? frame.t - m.enterT : 0;
			if (
				shouldWake(
					{
						enabled: frame.enabled,
						fromEdgePx: fe,
						buttonsHeld: frame.buttons !== 0,
						appDrag: frame.appDrag,
						armed: frame.armed,
						dwellMs
					},
					frame.params
				)
			) {
				m.awake = true;
				m.activeSince = frame.t;
				m.leaveT = 0;
			}
		} else {
			m.enterT = 0;
		}
	} else {
		const reach = stayReachPx(
			frame.pointerY,
			frame.top,
			frame.headH,
			frame.tabH,
			m.headW,
			m.widths,
			frame.tabCount
		);
		if (frame.hold === true) {
			m.leaveT = 0;
		} else if (!frame.inWindow || fe > reach || !frame.enabled) {
			if (m.leaveT === 0) m.leaveT = frame.t;
			if (frame.t - m.leaveT > LEAVE_MS) {
				m.awake = false;
				m.enterT = 0;
				m.leaveT = 0;
			}
		} else {
			m.leaveT = 0;
		}
	}

	const visualAwake = m.awake || anyHeldVisible;
	if (m.enterT !== 0) busy = true;
	if (m.leaveT !== 0) busy = true;

	const headTarget = visualAwake ? HEAD_OPEN_W : REST_W;
	m.headW = easeWidth(frame.headW, headTarget, frame.dtMs, frame.params, frame.reduceMotion);
	if (m.headW !== headTarget) busy = true;

	for (let i = 0; i < frame.tabCount; i++) {
		const cy = frame.pointerY - (frame.top + frame.headH + 4 + i * frame.tabH + frame.tabH / 2);
		const ramp = frame.reduceMotion ? 1 : flipRamp(frame.t, m.flipT, i);
		if (visualAwake && ramp < 1) busy = true;
		const target = tabTargetWidth(
			{
				awake: visualAwake,
				dy: cy,
				tabH: frame.tabH,
				held: frame.held[i] ?? false,
				filled: frame.filled[i] ?? false,
				ramp,
				searchOpen: visualAwake && frame.searchOpen === true,
				matches: frame.matches?.[i] ?? true,
				editingOther: frame.editingOther?.[i] ?? false
			},
			frame.params
		);
		const cur = frame.widths[i] ?? REST_W;
		const next = easeWidth(cur, target, frame.dtMs, frame.params, frame.reduceMotion);
		m.widths[i] = next;
		if (next !== target) busy = true;
	}
	m.needsFrame = busy || visualAwake;
}

export function motionIdle(m: EdgeMotionResult): boolean {
	return !m.needsFrame && !m.awake && m.enterT === 0 && m.leaveT === 0;
}
