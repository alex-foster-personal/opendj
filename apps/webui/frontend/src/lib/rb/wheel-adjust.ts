import { api } from '../api/client';
import { makeDiskWriteChain } from './disk-write-chain';

/**
 * Mouse-wheel adjust for continuous 0..1 controls (dials and faders).
 *
 * One Svelte action, applied to a control's interactive element, so every
 * dial and fader gains scroll-to-adjust without each component growing its
 * own wheel handler. Wheel up raises, wheel down lowers.
 *
 * Direction-only, not magnitude-scaled: a DJ needs one notch to mean the
 * same amount every time, and trackpad deltaY magnitudes vary wildly
 * between devices and momentum phases.
 *
 * INPUT-KIND SENSITIVITY (the maintainer, Mon 31 Aug 2026, in-app review notes)
 * Because the handler is direction-only, one wheel EVENT moves a control by
 * one full step. That is correct for a notched mouse wheel, which emits one
 * event per physical detent. A macOS trackpad two-finger scroll emits a dense
 * burst of high-frequency events for a single finger movement, so the same
 * gesture walked the control several steps and read as hypersensitive. He
 * reported it first for the dials (14:50Z) and then for the channel level
 * faders (15:10Z), asking for a GLOBAL adjustment rather than a per-control
 * tweak, with trackpads scaled separately from mice.
 *
 * The fix is therefore applied HERE, in the one action every control already
 * uses, by scaling the step by a per-input-kind factor. Direction-only
 * semantics are preserved: deltaY magnitude is still never read as a value.
 *
 * Requirements:
 *   ✔︎ Scrolling a control changes it without scrolling the page.
 *     [if] the pointer is over a dial and the wheel turns [then] the value
 *       moves by exactly one step and the panel underneath does not scroll
 *   ✔︎ Values stay inside the control's 0..1 domain.
 *     [if] the wheel keeps turning at either end [then] the value pins to
 *       0 or 1 and never leaves the range ⛔️
 *   ✔︎ Inert / read-only controls ignore the wheel entirely.
 *     [if] a control passes disabled [then] no set() call is made and the
 *       page scroll is left alone
 *   ✔︎ A trackpad gesture moves a control by about as much as the equivalent
 *     mouse gesture, on every wheel-adjustable control at once.
 *     [if] a trackpad burst of WHEEL_TRACKPAD_EVENTS_PER_DETENT events and a
 *       single mouse detent are dispatched at the same control [then] the two
 *       totals agree to within a rounding epsilon ⛔️
 *     [if] a control is added with use:wheelAdjust and no sensitivity code of
 *       its own [then] it still gets the trackpad scaling ⛔️
 *   ✔︎ The factors are configurable at runtime and persist across reloads.
 *     [if] setWheelSensitivity runs and the page reloads [then] the new factor
 *       is still in force ⛔️
 *     [if] a stored blob is malformed [then] it throws loudly rather than
 *       silently resetting to the default ⛔️
 *   ✔︎ A user can retune both factors from the settings panel, no devtools.
 *     [if] the wheel sensitivity row's slider moves [then] the factor for that
 *       input kind changes and persists, and every wheel-adjustable control
 *       obeys it on the next scroll ⛔️
 *     [if] the same factor is written through window.__mdtWheelSensitivity
 *       instead [then] the slider readout and the runtime agree, one store ⛔️
 *   ✔︎ WHEEL_TRACKPAD_EVENTS_PER_DETENT stays the one place the ratio is
 *     stated, so the default 3x survives being made user-editable.
 *     [if] the trackpad factor is reset [then] it is again the reciprocal of
 *       the events-per-detent constant ⛔️
 *   ✔︎ Making the floor a write-time rule never bricks an existing install.
 *     [if] a blob stored before the floor existed holds a factor below it
 *       [then] the factor is raised to the floor and the migrated blob is
 *       written back, and a fresh load reads the raised value ⛔️
 *     [if] a stored factor was never legal in any version (non-finite, zero,
 *       negative, or above the ceiling) [then] it still throws loudly ⛔️
 *   ✔︎ Anything that changes the factors tells the settings panel, whoever
 *     changed them.
 *     [if] the agent bridge writes or resets while the panel is open [then]
 *       every subscriber is told, so the slider and its readout cannot keep
 *       showing the previous factor ⛔️
 */

//-----------------------------------------------------------------------------
// config
//-----------------------------------------------------------------------------

/** Per-control-type wheel step, in 0..1 control units. */
export const WHEEL_STEP = {
	/** Dials: trim, EQ bands, filter, headphone mix/level. */
	knob: 0.028,
	/** Channel level faders. */
	fader: 0.028,
	/** Crossfader - coarser, it travels a shorter useful distance. */
	crossfader: 0.04,
	/** Pitch fader: 0.01 of full travel = 0.16% at +-8, 0.32% at +-16. */
	pitch: 0.01
} as const;

/** Wheel input device classes. See detectWheelInputKind for how they differ. */
export type WheelInputKind = 'mouse' | 'trackpad';

/**
 * THE SENSITIVITY KNOB. Trackpad wheel events that add up to one mouse detent's
 * worth of travel. Change this one number and every wheel-adjustable control
 * retunes together: all dials, the channel level faders, the crossfader, the
 * pitch faders, the headphone mix/level, and the shift-selected dial driven by
 * the page-level wheel in knob-control.
 *
 * Derivation: the wheel handlers are direction-only, so one wheel EVENT moves a
 * control by one full step. A notched mouse wheel emits one event per physical
 * detent, which is the assumption that was built for. A macOS trackpad emits a
 * dense burst of events for a single two-finger movement, so the same gesture
 * walked the control several steps and read as hypersensitive. the maintainer calibrated
 * the ratio by hand on the Air, Mon 31 Aug 2026: "currently should be I think
 * 3x less sensitive for trackpads". It is a feel number rather than a derived
 * one, so it lives here as a single named constant instead of being buried in
 * a handler.
 */
export const WHEEL_TRACKPAD_EVENTS_PER_DETENT = 3;

/**
 * Per-input-kind multiplier applied to EVERY control's wheel step, derived from
 * the knob above. mouse = 1 is the reference: one detent, one full step, so
 * mouse behavior is exactly what it was before this seam existed.
 */
export const WHEEL_SENSITIVITY: Readonly<Record<WheelInputKind, number>> = {
	mouse: 1,
	trackpad: 1 / WHEEL_TRACKPAD_EVENTS_PER_DETENT
};

/**
 * Upper sanity bound for a configured factor. A typo like 300 would make
 * every control unusable in one scroll, so it throws instead.
 *
 * MIN and MAX are the single source for the settings panel's slider range
 * (lib/settings/catalog.ts builds the control's min/max/step from them), so a
 * slider position can never be a value the validator refuses, and widening the
 * validator widens the slider in the same edit.
 */
export const WHEEL_SENSITIVITY_MAX = 4;

/** Lower sanity bound for a configured factor. Zero is refused outright (a
 * factor of 0 makes every wheel-adjustable control inert while looking live),
 * and 0.05 is the smallest factor that still reads as movement rather than a
 * dead dial. */
export const WHEEL_SENSITIVITY_MIN = 0.05;

/** Slider granularity for the settings control, in factor units. */
export const WHEEL_SENSITIVITY_STEP = 0.05;

/**
 * Legacy WHEEL_DELTA quantum. Quantized ("notched") wheel devices report
 * wheelDeltaY in integer multiples of this; high-precision devices do not.
 */
export const WHEEL_DELTA_QUANTUM = 120;

/** WheelEvent.DOM_DELTA_PIXEL. Spelled out because synthetic events in tests
 * do not carry the WheelEvent statics. */
export const WHEEL_DELTA_MODE_PIXEL = 0;

const SENSITIVITY_STORAGE_KEY = 'mdt.rb.wheel-sensitivity.v1';

//-----------------------------------------------------------------------------
// input-kind detection
//-----------------------------------------------------------------------------

/**
 * Classify one wheel event as coming from a notched mouse wheel or a
 * high-precision trackpad.
 *
 * There is no standard flag for this: w3c/uievents#337 is the open request for
 * one, and browsers ship no isTrackpad property. The two signals below are the
 * established discriminators, and are used in the order that keeps each
 * browser on the signal that is actually reliable there.
 *
 * 1. deltaMode. Firefox reports DOM_DELTA_LINE for a notched wheel (it scrolls
 *    by lines) and DOM_DELTA_PIXEL for a trackpad. So a non-pixel deltaMode is
 *    an unambiguous mouse. Chromium and WebKit report PIXEL for BOTH devices on
 *    macOS, which is why deltaMode alone cannot carry the decision there.
 *
 * 2. wheelDeltaY quantization. Quantized devices (conventional mouse wheels
 *    with tactile detents) report wheelDeltaY in integer multiples of 120.
 *    High-precision devices (macOS trackpads, Magic Mouse) report values that
 *    vary smoothly with finger travel and are almost never multiples of 120.
 *    wheelDeltaY is non-standard and deprecated, but it is implemented across
 *    Chromium and WebKit, which is exactly where signal 1 is unavailable.
 *
 * Known and accepted limits, stated rather than smoothed over:
 * - A fast trackpad flick can land on an exact multiple of 120 and read as
 *   mouse for that ONE event. The cost is a single coarser step inside a burst
 *   of many, and the next event re-classifies correctly. Suppressing it would
 *   need cross-event history, which trades a rare visible glitch for a stateful
 *   heuristic that is wrong in more situations.
 * - macOS momentum (inertia) events after the fingers lift are, per Chromium
 *   issue 40704952, indistinguishable from user-initiated ones. They are
 *   correctly classified as trackpad and scaled with everything else, but they
 *   are not filtered out, because no reliable signal exists to filter them by.
 */
export function detectWheelInputKind(event: WheelEvent): WheelInputKind {
	if (event.deltaMode !== WHEEL_DELTA_MODE_PIXEL) return 'mouse';
	const quantized = (event as WheelEvent & { wheelDeltaY?: number }).wheelDeltaY;
	// Pixel deltaMode with no wheelDeltaY at all is Firefox's trackpad: signal 1
	// already ruled out its notched wheel, so this is a classification, not a
	// fallback standing in for a failed detection.
	if (typeof quantized !== 'number' || quantized === 0) return 'trackpad';
	return Math.abs(quantized) % WHEEL_DELTA_QUANTUM === 0 ? 'mouse' : 'trackpad';
}

//-----------------------------------------------------------------------------
// sensitivity state
//-----------------------------------------------------------------------------

/** null means "there is no persistence here", which is a real state during SSR
 * and in unit tests rather than a swallowed failure: the caller falls back to
 * the declared defaults and says so. */
function _storage(): Storage | null {
	if (typeof window === 'undefined') return null;
	return window.localStorage ?? null;
}

/**
 * The subset of the factor contract that has held in EVERY version of this
 * module, and therefore the only thing a STORED value may be judged against.
 *
 * The floor is deliberately excluded: `WHEEL_SENSITIVITY_MIN` arrived with the
 * settings control (issue #613), AFTER the agent bridge shipped in #599, so a
 * stored factor between 0 and the floor is a value an earlier version of this
 * module wrote and accepted. It is stale, not corrupt. `_loadSensitivity`
 * migrates it rather than refusing it, because this module is imported by the
 * settings catalog and every performance control, so throwing here would stop
 * the UI loading at import time for anyone who had already retuned from
 * devtools (review thread r3984202673).
 *
 * Anything that fails THIS check was never writable through any version, so it
 * is corruption and still throws.
 */
function _assertStoredFactor(kind: WheelInputKind, factor: number): void {
	if (typeof factor !== 'number' || !Number.isFinite(factor) || factor <= 0) {
		throw new RangeError(
			`wheel sensitivity for '${kind}' must be a positive finite number, got ${factor}`
		);
	}
	if (factor > WHEEL_SENSITIVITY_MAX) {
		throw new RangeError(
			`wheel sensitivity for '${kind}' must be <= ${WHEEL_SENSITIVITY_MAX}, got ${factor}`
		);
	}
}

function _assertFactor(kind: WheelInputKind, factor: number): void {
	_assertStoredFactor(kind, factor);
	if (factor < WHEEL_SENSITIVITY_MIN) {
		throw new RangeError(
			`wheel sensitivity for '${kind}' must be >= ${WHEEL_SENSITIVITY_MIN}, got ${factor}`
		);
	}
}

/**
 * A MISSING key is the real first-run state and yields WHEEL_SENSITIVITY; a
 * PRESENT but malformed blob throws loudly rather than silently resetting, so a
 * corrupted value cannot masquerade as "he never configured it". Same policy as
 * rb/prefs.svelte.ts.
 *
 * A stored factor below the floor is the one exception, and it is a migration
 * rather than a tolerance: it is raised to the floor and the migrated blob is
 * written back immediately, so the stored value and the settings slider (whose
 * minimum is that same floor) cannot end up disagreeing about what is set.
 * There is no path by which a NEW value below the floor can be stored --
 * `setWheelSensitivity` still refuses one -- so this only ever runs once, on
 * the first load after the upgrade.
 */
function _loadSensitivity(): Record<WheelInputKind, number> {
	const storage = _storage();
	if (storage === null) return { ...WHEEL_SENSITIVITY };
	const raw = storage.getItem(SENSITIVITY_STORAGE_KEY);
	if (raw === null) return { ...WHEEL_SENSITIVITY };
	const parsed = JSON.parse(raw) as Partial<Record<WheelInputKind, number>>;
	if (parsed === null || typeof parsed !== 'object') {
		throw new Error(
			`${SENSITIVITY_STORAGE_KEY}: malformed blob (must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const next = { ...WHEEL_SENSITIVITY } as Record<WheelInputKind, number>;
	let migrated = false;
	for (const kind of ['mouse', 'trackpad'] as const) {
		const value = parsed[kind];
		if (value === undefined) continue;
		try {
			_assertStoredFactor(kind, value);
		} catch (cause) {
			throw new Error(
				`${SENSITIVITY_STORAGE_KEY}: malformed blob (${(cause as Error).message}) - ` +
					'clear the localStorage key to recover'
			);
		}
		if (value < WHEEL_SENSITIVITY_MIN) {
			next[kind] = WHEEL_SENSITIVITY_MIN;
			migrated = true;
		} else {
			next[kind] = value;
		}
	}
	if (migrated) storage.setItem(SENSITIVITY_STORAGE_KEY, JSON.stringify(next));
	return next;
}

let _sensitivity: Record<WheelInputKind, number> = _loadSensitivity();

export type WheelSensitivityDisk = Partial<Record<WheelInputKind, number>>;

const _syncWheelSensitivityDisk = makeDiskWriteChain(
	async (patch: { wheel_sensitivity: Record<WheelInputKind, number> }) => {
		try {
			await api.PUT('/api/v1/ui-prefs', { body: patch });
		} catch {
			/* localStorage remains authoritative if daemon is down */
		}
	}
);

function _persistSensitivity(): void {
	_storage()?.setItem(SENSITIVITY_STORAGE_KEY, JSON.stringify(_sensitivity));
}

function _syncWheelSensitivityToDisk(): void {
	void _syncWheelSensitivityDisk({
		wheel_sensitivity: { mouse: _sensitivity.mouse, trackpad: _sensitivity.trackpad }
	});
}

/** Apply disk-backed wheel factors from GET /api/v1/ui-prefs (issue #2854). */
export function hydrateWheelSensitivityFromDisk(body: {
	wheel_sensitivity?: WheelSensitivityDisk;
}): void {
	const raw = body.wheel_sensitivity;
	if (raw === undefined || typeof raw !== 'object') return;
	const next = { ..._sensitivity };
	let changed = false;
	for (const kind of ['mouse', 'trackpad'] as const) {
		const value = raw[kind];
		if (typeof value !== 'number' || !Number.isFinite(value)) continue;
		try {
			_assertStoredFactor(kind, value);
		} catch {
			continue;
		}
		const applied = value < WHEEL_SENSITIVITY_MIN ? WHEEL_SENSITIVITY_MIN : value;
		if (next[kind] !== applied) {
			next[kind] = applied;
			changed = true;
		}
	}
	if (!changed) return;
	_sensitivity = next;
	_persistSensitivity();
	_notifySensitivityChange();
}

/** Current factors. Copy, so callers cannot mutate the live config in place. */
export function wheelSensitivity(): Record<WheelInputKind, number> {
	return { ..._sensitivity };
}

type SensitivityListener = (next: Record<WheelInputKind, number>) => void;

const _listeners = new Set<SensitivityListener>();

function _notifySensitivityChange(): void {
	for (const listener of [..._listeners]) listener({ ..._sensitivity });
}

/**
 * Subscribe to every change of the factors, whoever made it: the settings
 * panel's slider, the agent bridge, or a reset. Returns an unsubscribe.
 *
 * This exists because the store is a plain module variable in a `.ts` file, so
 * Svelte cannot track it. Without a subscription, the settings overlay's own
 * draft mirror is only correct for writes that came through its own mutator,
 * and shows the previous factor after `window.__mdtWheelSensitivity.set()` or
 * `.reset()` runs while the panel is open -- two answers for one setting
 * (review thread r3984202679). The listener is told WHEN the factors changed,
 * never what to render: each caller owns its own render state.
 *
 * Listeners are called synchronously and in registration order, and the copy
 * passed to each one is the caller's own.
 */
export function subscribeWheelSensitivity(listener: SensitivityListener): () => void {
	_listeners.add(listener);
	return () => {
		_listeners.delete(listener);
	};
}

/** Set one input kind's factor and persist it. Throws on a value outside
 * (WHEEL_SENSITIVITY_MIN, WHEEL_SENSITIVITY_MAX]. */
export function setWheelSensitivity(kind: WheelInputKind, factor: number): void {
	if (kind !== 'mouse' && kind !== 'trackpad') {
		throw new TypeError(`wheel sensitivity kind must be 'mouse'|'trackpad', got ${kind}`);
	}
	_assertFactor(kind, factor);
	_sensitivity[kind] = factor;
	_persistSensitivity();
	_syncWheelSensitivityToDisk();
	_notifySensitivityChange();
}

/** Drop any configured override and go back to WHEEL_SENSITIVITY. */
export function resetWheelSensitivity(): void {
	_sensitivity = { ...WHEEL_SENSITIVITY };
	_storage()?.removeItem(SENSITIVITY_STORAGE_KEY);
	_syncWheelSensitivityToDisk();
	_notifySensitivityChange();
}

/**
 * Agent-native parity (house rule: every UI-reachable behavior gets a non-UI
 * path). `window.__mdtWheelSensitivity` lets a headless agent read and drive
 * the same factors from outside the ES module scope, matching the existing
 * `__mdtMasterMute` / `__mdtPerfLog` bridges.
 */
export function installWheelSensitivityGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & {
		__mdtWheelSensitivity?: {
			get: () => Record<WheelInputKind, number>;
			set: (kind: WheelInputKind, factor: number) => void;
			reset: () => void;
			defaults: () => Record<WheelInputKind, number>;
		};
	};
	w.__mdtWheelSensitivity = {
		get: () => wheelSensitivity(),
		set: (kind, factor) => setWheelSensitivity(kind, factor),
		reset: () => resetWheelSensitivity(),
		defaults: () => ({ ...WHEEL_SENSITIVITY })
	};
}

installWheelSensitivityGlobal();

//-----------------------------------------------------------------------------
// action
//-----------------------------------------------------------------------------

export interface WheelAdjustParams {
	/** Value change per wheel notch, in 0..1 units. Must be finite and > 0. */
	step: number;
	/** Current value, read at wheel time so it is never stale. */
	get: () => number;
	/** Receives the clamped next value. */
	set: (value: number) => void;
	/** Inert controls opt out and let the page scroll normally. */
	disabled?: boolean;
}

function _clamp01(value: number): number {
	return Math.min(1, Math.max(0, value));
}

function _assertParams(params: WheelAdjustParams): void {
	if (!Number.isFinite(params.step) || params.step <= 0) {
		throw new RangeError(`wheelAdjust step must be a positive finite number, got ${params.step}`);
	}
	if (typeof params.get !== 'function' || typeof params.set !== 'function') {
		throw new TypeError('wheelAdjust requires get() and set() functions');
	}
}

/** Wheel direction as -1 / 0 / +1. Vertical wins; deltaX covers shift-scroll. */
export function wheelDirection(event: WheelEvent): -1 | 0 | 1 {
	const delta = event.deltaY !== 0 ? event.deltaY : event.deltaX;
	if (delta < 0) return 1;
	if (delta > 0) return -1;
	return 0;
}

/** Horizontal wheel direction when |deltaX| dominates (MIXUX-08 AC7). */
export function horizontalWheelDirection(event: WheelEvent): -1 | 0 | 1 {
	if (event.deltaX === 0) return 0;
	if (Math.abs(event.deltaY) >= Math.abs(event.deltaX)) return 0;
	if (event.deltaX < 0) return 1;
	if (event.deltaX > 0) return -1;
	return 0;
}

/** A control's declared step scaled for the device that produced the event.
 * Pure, so the scaling is testable without a DOM. */
export function scaledWheelStep(step: number, kind: WheelInputKind): number {
	return step * _sensitivity[kind];
}

export function wheelAdjust(node: Element, params: WheelAdjustParams) {
	_assertParams(params);
	let current = params;

	function onWheel(event: WheelEvent): void {
		if (current.disabled === true) return;
		const direction = wheelDirection(event);
		if (direction === 0) return;
		event.preventDefault();
		event.stopPropagation();
		const step = scaledWheelStep(current.step, detectWheelInputKind(event));
		const next = _clamp01(current.get() + direction * step);
		current.set(next);
	}

	node.addEventListener('wheel', onWheel as EventListener, { passive: false });

	return {
		update(next: WheelAdjustParams): void {
			_assertParams(next);
			current = next;
		},
		destroy(): void {
			node.removeEventListener('wheel', onWheel as EventListener);
		}
	};
}
