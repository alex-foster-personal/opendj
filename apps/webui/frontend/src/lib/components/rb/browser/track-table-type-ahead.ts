/**
 * LIBM-82b (issue #12317): type-ahead JUMP in the track table, distinct from filtering.
 *
 * Typing on a focused track row moves the selection to the next visible row whose title starts
 * with the typed prefix; the search box and every filter are left alone. WAI-ARIA APG listbox
 * type-ahead, mimicked: keys within TYPE_AHEAD_RESET_MS of each other build one prefix, a repeated
 * single letter cycles through the rows starting with it, matching wraps, and no match leaves the
 * selection where it was.
 *
 * The app's single-letter shortcuts keep their meaning: a key another handler already claimed
 * (deck C, V, K, P, M, digits) and the browser's W, A, S, D navigation never START a prefix. Once a
 * prefix is open, every printable key extends it and the global shortcuts yield
 * (`typeAheadOwnsKey`), so "disco" never fires the deck cue on its "c".
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

/** Gap between keys after which a new prefix starts (Chromium's select type-ahead, 1 s, mimicked). */
export const TYPE_AHEAD_RESET_MS = 1000;

/** Longest prefix the agent command accepts. */
export const TYPE_AHEAD_MAX_PREFIX = 64;

const BROWSER_NAV_KEYS = new Set(['w', 'a', 's', 'd']);

export interface TypeAheadKeyEvent {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	isComposing?: boolean;
	defaultPrevented: boolean;
	timeStamp: number;
	target: EventTarget | null;
	currentTarget: EventTarget | null;
}

/** One table's jump, registered while a track table is mounted, for the agent command. */
export interface TypeAheadTarget {
	/** Select the row `prefix` jumps to; its stable_id, or null when no visible row matches. */
	jump(prefix: string): string | null;
}

// ---------------------------------------------------------------------------
// Pure matching

/** Title text as type-ahead compares it: accents dropped, case folded, leading space ignored. */
export function typeAheadFold(text: string): string {
	return text
		.normalize('NFKD')
		.replace(/\p{M}+/gu, '')
		.toLocaleLowerCase()
		.trimStart();
}

/**
 * Index of the row `prefix` jumps to from row `current` (-1: none active), or -1 when no row
 * matches. A repeated single letter ("a", "aa") looks from the row after `current`, so pressing
 * it again cycles; a longer prefix looks from `current` itself, so typing on keeps the row.
 */
export function typeAheadMatchIndex(
	titles: readonly (string | null)[],
	prefix: string,
	current: number
): number {
	const folded = typeAheadFold(prefix);
	const count = titles.length;
	if (folded === '' || count === 0) return -1;
	const first = [...folded][0];
	const repeated = [...folded].every((ch) => ch === first);
	const needle = repeated ? first : folded;
	const from = current >= 0 && current < count ? current : -1;
	const start = repeated ? from + 1 : Math.max(from, 0);
	for (let step = 0; step < count; step++) {
		const index = (start + step) % count;
		if (typeAheadFold(titles[index] ?? '').startsWith(needle)) return index;
	}
	return -1;
}

function _isPrintableKey(e: TypeAheadKeyEvent): boolean {
	if (e.isComposing === true || e.metaKey || e.ctrlKey || e.altKey) return false;
	return [...e.key].length === 1 && /^\P{C}$/u.test(e.key);
}

function _isTrackRow(target: EventTarget | null): boolean {
	const el = target as { closest?: (selector: string) => unknown } | null;
	return typeof el?.closest === 'function' && el.closest('[data-testid="track-row"]') !== null;
}

// ---------------------------------------------------------------------------
// The prefix session

/** The prefix being typed and when its last key landed. */
export class TrackTypeAhead {
	#prefix = '';
	#lastAt = Number.NEGATIVE_INFINITY;
	#target: TypeAheadTarget | null = null;

	/** True while a prefix is open: its last key landed under TYPE_AHEAD_RESET_MS ago. */
	isOpen(now: number): boolean {
		return this.#prefix !== '' && now - this.#lastAt < TYPE_AHEAD_RESET_MS;
	}

	/** The character a row keydown adds to the prefix, or null when the key is not type-ahead's. */
	charFor(e: TypeAheadKeyEvent): string | null {
		if (e.defaultPrevented || !_isPrintableKey(e)) return null;
		if (isTextEntryTarget(e.target) || e.target !== e.currentTarget) return null;
		if (this.isOpen(e.timeStamp)) return e.key;
		if (e.key === ' ' || BROWSER_NAV_KEYS.has(e.key.toLowerCase())) return null;
		return e.key;
	}

	/** Add one typed character; the whole prefix it makes (a new one after the reset gap). */
	push(char: string, now: number): string {
		if (!this.isOpen(now)) this.#prefix = '';
		this.#prefix += char;
		this.#lastAt = now;
		return this.#prefix;
	}

	/** True when a global shortcut must leave this key to an open prefix on a track row. */
	ownsKey(e: TypeAheadKeyEvent): boolean {
		return (
			this.isOpen(e.timeStamp) &&
			_isPrintableKey(e) &&
			!isTextEntryTarget(e.target) &&
			_isTrackRow(e.target)
		);
	}

	/** Register the mounted table's jump; the returned function unregisters it. */
	register(target: TypeAheadTarget): () => void {
		this.#target = target;
		return () => {
			if (this.#target === target) this.#target = null;
		};
	}

	/** The mounted table's jump, or null with no track table mounted. */
	get target(): TypeAheadTarget | null {
		return this.#target;
	}
}

/** The app's one type-ahead session: one track table takes keys at a time. */
export const trackTypeAhead = new TrackTypeAhead();

/** Validate the agent command's prefix: a non-blank string of at most TYPE_AHEAD_MAX_PREFIX characters. */
export function parseTypeAheadPrefix(value: unknown): string {
	if (typeof value !== 'string' || value.trim() === '') {
		throw new TypeError('prefix must be a non-blank string');
	}
	if ([...value].length > TYPE_AHEAD_MAX_PREFIX) {
		throw new TypeError(`prefix must be at most ${TYPE_AHEAD_MAX_PREFIX} characters`);
	}
	return value;
}
