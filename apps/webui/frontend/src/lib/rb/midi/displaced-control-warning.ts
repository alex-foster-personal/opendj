/**
 * PVPIN-12193 (PVPIN-11): bounded, configurable warning for displaced MIDI controls.
 *
 * Pure scheduler: the takeover runtime feeds it how many absolute controls
 * still show a pickup ghost (the rejoin arrows), and it decides when the small
 * orange toast is due. It shows at once, then again every 30 s until every
 * control resynchronizes, and never once the DJ turned it off in config.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 update(): due on the first displaced sample, then every REPEAT_MS.
 *     [if] a control stays displaced past REPEAT_MS and no second warning is due [then ⛔️] broken
 *     [if] a resync before REPEAT_MS still leaves a repeat pending [then ⛔️] broken
 *   ✔︎ disable(): persists the off switch and stops every later warning.
 *     [if] a disabled warning is due again, or after a reload [then ⛔️] broken
 *   ✔︎ enable(): the MIDI panel toggle (or midi_displaced_warning) turns it back on.
 *     [if] a re-enabled warning stays silent while a control is displaced [then ⛔️] broken
 */

/** How long one warning toast stays up. */
export const DISPLACED_WARNING_SHOW_MS = 2_000;
/** How often the warning returns while a control stays displaced. */
export const DISPLACED_WARNING_REPEAT_MS = 30_000;
export const DISPLACED_WARNING_KEY = 'opendj.midi.displaced-warning';
export const DISPLACED_WARNING_MESSAGE =
	'A MIDI slider or knob is displaced from the software position. Click to turn this warning off.';

export interface DisplacedWarningStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
}

export interface DisplacedWarningStep {
	/** Show the toast now. */
	show: boolean;
	/** When to call update() again, or null when nothing is pending. */
	nextCheckAt: number | null;
}

/** Read the persisted switch; the warning is on unless the DJ turned it off. */
export function readDisplacedWarningEnabled(storage: DisplacedWarningStorage | null): boolean {
	return storage?.getItem(DISPLACED_WARNING_KEY) !== 'off';
}

/** Persist the switch (the MIDI panel toggle, the bus command and the toast's Turn off). */
export function writeDisplacedWarningEnabled(
	storage: DisplacedWarningStorage | null,
	enabled: boolean
): void {
	storage?.setItem(DISPLACED_WARNING_KEY, enabled ? 'on' : 'off');
}

export class DisplacedControlWarning {
	#enabled: boolean;
	#lastShownAt: number | null = null;
	readonly #storage: DisplacedWarningStorage | null;

	/** Seed the switch from storage (null storage means on, nothing persisted). */
	constructor(storage: DisplacedWarningStorage | null) {
		this.#storage = storage;
		this.#enabled = readDisplacedWarningEnabled(storage);
	}

	get enabled(): boolean {
		return this.#enabled;
	}

	/** The toast click: turn the warning off and persist that in config. */
	disable(): void {
		this.#enabled = false;
		this.#lastShownAt = null;
		writeDisplacedWarningEnabled(this.#storage, false);
	}

	/** The MIDI panel toggle turned back on: the next displaced sample warns at once. */
	enable(): void {
		this.#enabled = true;
		this.#lastShownAt = null;
		writeDisplacedWarningEnabled(this.#storage, true);
	}

	/** Decide whether the warning is due for `displacedCount` displaced controls at `now` ms. */
	update(displacedCount: number, now: number): DisplacedWarningStep {
		if (!this.#enabled || displacedCount <= 0) {
			this.#lastShownAt = null;
			return { show: false, nextCheckAt: null };
		}
		if (this.#lastShownAt === null || now - this.#lastShownAt >= DISPLACED_WARNING_REPEAT_MS) {
			this.#lastShownAt = now;
			return { show: true, nextCheckAt: now + DISPLACED_WARNING_REPEAT_MS };
		}
		return { show: false, nextCheckAt: this.#lastShownAt + DISPLACED_WARNING_REPEAT_MS };
	}
}
