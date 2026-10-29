/**
 * Click-drag sensitivity shared by every mixer knob and fader drag, single or
 * grouped (R5, Wed 7 Oct 2026). the maintainer: "click drag in general get less
 * sensitive the further from the click it gets and can sensitivity be 30%
 * lower as base".
 *
 * gain(d) = base / (1 + d / falloffPx), where d is the pointer distance in px
 * from the press point. It is INTEGRATED frame by frame (value = start +
 * sum(step_i * gain(d_i))), so moving back toward the press point gives fine
 * control back and nothing ever jumps.
 */

export const DRAG_DEFAULTS = {
	/** Multiplier on the pre-R5 drag scale: "30% lower as base". */
	base: 0.7,
	/** Distance in px at which the gain has halved. */
	falloffPx: 150
} as const;

/** Settings slider bounds (R6); the validator and the slider share them. */
export const DRAG_BASE_MIN = 0.1;
export const DRAG_BASE_MAX = 2;
export const DRAG_BASE_STEP = 0.05;
export const DRAG_FALLOFF_MIN_PX = 25;
export const DRAG_FALLOFF_MAX_PX = 1000;
export const DRAG_FALLOFF_STEP_PX = 25;

export interface DragSensitivityPrefs {
	base: number;
	falloff_px: number;
}

/** Throws on a value the settings slider could never produce. */
export function validateDragSensitivity(raw: unknown, where: string): DragSensitivityPrefs {
	if (raw === null || typeof raw !== 'object') throw new Error(`${where}: drag_sensitivity must be an object`);
	const { base, falloff_px } = raw as Record<string, unknown>;
	if (typeof base !== 'number' || !(base >= DRAG_BASE_MIN && base <= DRAG_BASE_MAX)) {
		throw new RangeError(`${where}: drag_sensitivity.base must be ${DRAG_BASE_MIN}..${DRAG_BASE_MAX}, got ${String(base)}`);
	}
	if (
		typeof falloff_px !== 'number' ||
		!(falloff_px >= DRAG_FALLOFF_MIN_PX && falloff_px <= DRAG_FALLOFF_MAX_PX)
	) {
		throw new RangeError(
			`${where}: drag_sensitivity.falloff_px must be ${DRAG_FALLOFF_MIN_PX}..${DRAG_FALLOFF_MAX_PX}, got ${String(falloff_px)}`
		);
	}
	return { base, falloff_px };
}

export const dragSensitivity = $state<{ base: number; falloffPx: number }>({
	base: DRAG_DEFAULTS.base,
	falloffPx: DRAG_DEFAULTS.falloffPx
});

/** Live-apply persisted prefs; every drag reads this state, so no reload is needed. */
export function applyDragSensitivity(prefs: DragSensitivityPrefs): void {
	const ok = validateDragSensitivity(prefs, 'applyDragSensitivity');
	dragSensitivity.base = ok.base;
	dragSensitivity.falloffPx = ok.falloff_px;
}

/** Pure gain at a pointer distance from the press point. */
export function dragGain(distancePx: number, base: number, falloffPx: number): number {
	if (!Number.isFinite(base) || base < 0) throw new RangeError(`drag base must be >= 0, got ${base}`);
	if (!(falloffPx > 0)) throw new RangeError(`drag falloff must be > 0 px, got ${falloffPx}`);
	return base / (1 + Math.max(0, distancePx) / falloffPx);
}

export interface DragTracker {
	pressX: number;
	pressY: number;
	lastX: number;
	lastY: number;
}

export function createDragTracker(x: number, y: number): DragTracker {
	return { pressX: x, pressY: y, lastX: x, lastY: y };
}

/**
 * Gained pointer increment since the last frame, in px. `dx` is
 * right-positive, `dy` UP-positive. The gain is taken at the distance of the
 * segment midpoint from the press point.
 */
export function trackDragMove(
	tracker: DragTracker,
	x: number,
	y: number,
	cfg: { base: number; falloffPx: number } = dragSensitivity
): { dx: number; dy: number } {
	const midX = (tracker.lastX + x) / 2 - tracker.pressX;
	const midY = (tracker.lastY + y) / 2 - tracker.pressY;
	const gain = dragGain(Math.hypot(midX, midY), cfg.base, cfg.falloffPx);
	const out = { dx: (x - tracker.lastX) * gain, dy: (tracker.lastY - y) * gain };
	tracker.lastX = x;
	tracker.lastY = y;
	return out;
}

/** Integration steps a straight agent drag may take, at most. */
export const MAX_STRAIGHT_DRAG_STEPS = 10_000;

/**
 * Gained travel of a straight drag from the press point, `dx` right-positive
 * and `dy` UP-positive, as a pointer moving along it would accumulate through
 * {@link trackDragMove} (integrated in steps of at most 1 px). Agent-native
 * drags go through this so they honor the same drag sensitivity as the UI
 * (Sol P1, PR #6829).
 */
export function gainedStraightDrag(
	dx: number,
	dy: number,
	cfg: { base: number; falloffPx: number } = dragSensitivity
): { dx: number; dy: number } {
	if (!Number.isFinite(dx) || !Number.isFinite(dy)) {
		throw new RangeError(`drag travel must be finite, got ${dx}, ${dy}`);
	}
	// 1 px steps up to a few screens of travel; past that the step grows, so a
	// huge but finite agent drag costs bounded main-thread time (Sol P2, PR #6829).
	const steps = Math.min(
		MAX_STRAIGHT_DRAG_STEPS,
		Math.max(1, Math.ceil(Math.max(Math.abs(dx), Math.abs(dy))))
	);
	const tracker = createDragTracker(0, 0);
	const out = { dx: 0, dy: 0 };
	for (let i = 1; i <= steps; i += 1) {
		// Screen y grows downward, so an UP-positive dy is a negative clientY.
		const step = trackDragMove(tracker, (dx * i) / steps, (-dy * i) / steps, cfg);
		out.dx += step.dx;
		out.dy += step.dy;
	}
	return out;
}
