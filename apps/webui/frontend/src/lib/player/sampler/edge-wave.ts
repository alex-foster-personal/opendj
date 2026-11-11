/**
 * Edge wave geometry and arming for the edge sampler ((private path) 7.1, 7.4).
 * Pure: no DOM, no clock reads except through arguments.
 */

export const REST_W = 3;
export const AWAKE_W = 32;
/** Top tab width when the edge is awake (S7 fills the shell). */
export const HEAD_OPEN_W = 200;
export const ZONE_PX = 14;
/** Extra px beyond a tab's popped width that keeps the wave open. */
export const STAY_PAD_PX = 16;
export const LEAVE_MS = 250;
export const OPEN_DELAY_MS = 60;
export const EASE_MS = 90;
/** Tabs per visible page (hotkeys 1 through 0 map here). */
export const TABS_PER_PAGE = 10;

export interface WaveParams {
	spread: number;
	pop: number;
	speed: number;
	delay: number;
}

export const DEFAULT_WAVE: WaveParams = {
	spread: 2.2,
	pop: 120,
	speed: EASE_MS,
	delay: OPEN_DELAY_MS
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
}

export function tabTargetWidth(t: TabWidthInput, p: WaveParams): number {
	if (!t.filled && !t.awake) return 0;
	const base = t.awake ? AWAKE_W : REST_W;
	if (!t.awake) return base;
	if (t.held) return p.pop;
	if (t.dy === 0) return p.pop;
	const k = t.dy / (p.spread * t.tabH);
	return base + (p.pop - base) * Math.exp(-k * k);
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
		if (!frame.inWindow || fe > reach || !frame.enabled) {
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
		const target = tabTargetWidth(
			{
				awake: visualAwake,
				dy: cy,
				tabH: frame.tabH,
				held: frame.held[i] ?? false,
				filled: frame.filled[i] ?? false
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
