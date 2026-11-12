import { api } from '../api/rb-http';
import type { components } from '../api-types';
import { makeDiskWriteChain } from './disk-write-chain';
import { detectWheelInputKind } from './wheel-adjust';

/**
 * Wheel distance for the library track list (pins 535c07d9fe88, ce142ae7e22f).
 *
 * At factor 1 the browser scrolls natively (LIBM-174). Below 1, a wheel event on
 * `.table-wrap` scales vertical deltaY and applies it on the main thread so one
 * notch moves fewer rows. Horizontal deltaX is never scaled.
 */

//-----------------------------------------------------------------------------
// config
//-----------------------------------------------------------------------------

export type LibraryListWheelKind = 'mouse' | 'trackpad';

/** Fraction of the browser wheel distance the track list moves; 1 = native scroll. */
export const LIBRARY_LIST_WHEEL_DEFAULTS: Readonly<Record<LibraryListWheelKind, number>> = {
	mouse: 0.5,
	trackpad: 0.5 / 3
};

export const LIBRARY_LIST_WHEEL_MIN = 0.05;
export const LIBRARY_LIST_WHEEL_MAX = 1;
export const LIBRARY_LIST_WHEEL_STEP = 0.05;

const _STORAGE_KEY = 'mdt.rb.library-list-wheel-sensitivity.v1';

//-----------------------------------------------------------------------------
// persistence
//-----------------------------------------------------------------------------

function _storage(): Storage | null {
	if (typeof localStorage === 'undefined') return null;
	return localStorage;
}

function _assertFactor(kind: LibraryListWheelKind, factor: number): void {
	if (
		!Number.isFinite(factor) ||
		factor <= LIBRARY_LIST_WHEEL_MIN ||
		factor > LIBRARY_LIST_WHEEL_MAX
	) {
		throw new RangeError(
			`library list wheel ${kind} must be in (${LIBRARY_LIST_WHEEL_MIN}, ${LIBRARY_LIST_WHEEL_MAX}], got ${factor}`
		);
	}
}

function _load(): Record<LibraryListWheelKind, number> {
	const storage = _storage();
	if (storage === null) return { ...LIBRARY_LIST_WHEEL_DEFAULTS };
	const raw = storage.getItem(_STORAGE_KEY);
	if (raw === null) return { ...LIBRARY_LIST_WHEEL_DEFAULTS };
	const parsed = JSON.parse(raw) as Partial<Record<LibraryListWheelKind, number>>;
	if (parsed === null || typeof parsed !== 'object') {
		throw new Error(`${_STORAGE_KEY}: malformed blob - clear the key to recover`);
	}
	const next = { ...LIBRARY_LIST_WHEEL_DEFAULTS };
	for (const kind of ['mouse', 'trackpad'] as const) {
		const value = parsed[kind];
		if (value === undefined) continue;
		_assertFactor(kind, value);
		next[kind] = value;
	}
	return next;
}

let _factors: Record<LibraryListWheelKind, number> = _load();

type Listener = (next: Record<LibraryListWheelKind, number>) => void;
const _listeners = new Set<Listener>();

function _notify(): void {
	const snap = libraryListWheelSensitivity();
	for (const listener of [..._listeners]) listener(snap);
}

function _persist(): void {
	_storage()?.setItem(_STORAGE_KEY, JSON.stringify(_factors));
}

export function libraryListWheelSensitivity(): Record<LibraryListWheelKind, number> {
	return { ..._factors };
}

export function subscribeLibraryListWheelSensitivity(listener: Listener): () => void {
	_listeners.add(listener);
	return () => {
		_listeners.delete(listener);
	};
}

const _syncLibraryListWheelDisk = makeDiskWriteChain(
	async (patch: components['schemas']['UiPrefsPatch']) => {
		try {
			await api.PUT('/api/v1/ui-prefs', { body: patch });
		} catch {
			/* localStorage remains authoritative if daemon is down */
		}
	}
);

function _syncDisk(): void {
	void _syncLibraryListWheelDisk({
		wheel_sensitivity: libraryListWheelDiskSnapshot()
	} as components['schemas']['UiPrefsPatch']);
}

export function setLibraryListWheelSensitivity(kind: LibraryListWheelKind, factor: number): void {
	_assertFactor(kind, factor);
	_factors[kind] = factor;
	_persist();
	_syncDisk();
	_notify();
}

export function resetLibraryListWheelSensitivity(): void {
	_factors = { ...LIBRARY_LIST_WHEEL_DEFAULTS };
	_storage()?.removeItem(_STORAGE_KEY);
	_syncDisk();
	_notify();
}

export type LibraryListWheelSensitivityDisk = Partial<Record<LibraryListWheelKind, number>>;

/** Keys `library_mouse` / `library_trackpad` on GET /api/v1/ui-prefs wheel_sensitivity. */
export function hydrateLibraryListWheelFromDisk(
	disk: LibraryListWheelSensitivityDisk | undefined
): void {
	if (disk === undefined || typeof disk !== 'object') return;
	const map: Record<string, LibraryListWheelKind> = {
		library_mouse: 'mouse',
		library_trackpad: 'trackpad'
	};
	const next = { ..._factors };
	let changed = false;
	for (const [diskKey, kind] of Object.entries(map)) {
		const value = disk[diskKey as keyof LibraryListWheelSensitivityDisk];
		if (typeof value !== 'number' || !Number.isFinite(value)) continue;
		try {
			_assertFactor(kind, value);
		} catch {
			continue;
		}
		if (next[kind] !== value) {
			next[kind] = value;
			changed = true;
		}
	}
	if (!changed) return;
	_factors = next;
	_persist();
	_notify();
}

export function libraryListWheelDiskSnapshot(): Record<string, number> {
	return {
		library_mouse: _factors.mouse,
		library_trackpad: _factors.trackpad
	};
}

//-----------------------------------------------------------------------------
// pure scroll math
//-----------------------------------------------------------------------------

const _DELTA_MODE_PIXEL = 0;

export interface LibraryWheelEventLike {
	deltaX: number;
	deltaY: number;
	deltaMode: number;
	ctrlKey: boolean;
}

export interface LibraryWheelScrollDelta {
	top: number;
	left: number;
}

/** Scroll delta for this wheel event, or null to leave scrolling to the browser. */
export function libraryWheelScrollDelta(
	event: LibraryWheelEventLike,
	factor: number
): LibraryWheelScrollDelta | null {
	if (
		!Number.isFinite(factor) ||
		factor <= LIBRARY_LIST_WHEEL_MIN ||
		factor > LIBRARY_LIST_WHEEL_MAX
	) {
		throw new Error(
			`libraryWheelScrollDelta: factor must be in (${LIBRARY_LIST_WHEEL_MIN}, ${LIBRARY_LIST_WHEEL_MAX}], got ${factor}`
		);
	}
	if (factor >= LIBRARY_LIST_WHEEL_MAX) return null;
	if (event.ctrlKey) return null;
	if (event.deltaMode !== _DELTA_MODE_PIXEL) return null;
	if (event.deltaX === 0 && event.deltaY === 0) return null;
	return { top: event.deltaY * factor, left: event.deltaX };
}

export function libraryWheelFactorForEvent(event: WheelEvent): number {
	const kind = detectWheelInputKind(event);
	return _factors[kind];
}

//-----------------------------------------------------------------------------
// action
//-----------------------------------------------------------------------------

/** Svelte action for the track list scroll container (`.table-wrap`). */
export function libraryWheelScroll(node: HTMLElement): { destroy: () => void } {
	let bound = false;
	const onWheel = (event: WheelEvent): void => {
		const factor = libraryWheelFactorForEvent(event);
		const delta = libraryWheelScrollDelta(event, factor);
		if (delta === null) return;
		event.preventDefault();
		node.scrollTop += delta.top;
		node.scrollLeft += delta.left;
	};
	const syncListener = (): void => {
		const needs =
			_factors.mouse < LIBRARY_LIST_WHEEL_MAX || _factors.trackpad < LIBRARY_LIST_WHEEL_MAX;
		if (needs && !bound) {
			node.addEventListener('wheel', onWheel, { passive: false });
			bound = true;
		} else if (!needs && bound) {
			node.removeEventListener('wheel', onWheel);
			bound = false;
		}
	};
	syncListener();
	const unsub = subscribeLibraryListWheelSensitivity(() => syncListener());
	return {
		destroy: () => {
			unsub();
			if (bound) node.removeEventListener('wheel', onWheel);
		}
	};
}
