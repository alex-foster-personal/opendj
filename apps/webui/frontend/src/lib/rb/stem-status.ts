/**
 * What the deck's stem row says about the stems (STEM-48).
 *
 * One pure mapping from the engine's stem read model to a NAMED state, a short
 * on-deck label, a hover explanation and the one action (if any) the user can
 * take. The row renders exactly this, so a stem button that is not live always
 * carries the reason, and the reason is the same text an agent reads.
 *
 *   ✔︎ ✅ 🎯 Every non-ready state has a name, a label and a hover reason.
 *       [if] a loading phase renders an empty label [then ⛔️]
 *       [if] two loading phases share a label [then ⛔️]
 *       [if] a failed load offers no retry [then ⛔️]
 *   ✔︎ ✅ 🎯 "No stems" is said only when it is true.
 *       [if] a mode that switches stems off reads as "no stems anywhere" [then ⛔️]
 *       [if] an empty deck reads as "this track has no stems" [then ⛔️]
 *       [if] a track with no bundle shows no hover reason [then ⛔️]
 */
import type { StemDeckState, StemFetchProgress } from '$lib/rb/stem-types';

export type StemStatusName =
	| 'ready'
	| 'probing'
	| 'fetching'
	| 'downloading'
	| 'decoding'
	| 'waiting'
	| 'switching'
	| 'error'
	| 'none'
	| 'off'
	| 'empty';

/** `retry` re-runs a failed load; `load_now` releases a load that is waiting. */
export type StemStatusAction = 'retry' | 'load_now';

export interface StemStatusView {
	name: StemStatusName;
	/** Short text for the deck itself; empty when the chips speak for themselves. */
	label: string;
	/** Hover text: what is happening and what the user can do about it. */
	tip: string;
	action: StemStatusAction | null;
	/** A load is in flight and will move on by itself. */
	busy: boolean;
}

const STEMS_DISABLED_PREFIX = 'stems disabled: ';
const CHIPS_ARRIVE = 'The deck already plays; the stem buttons switch on when this finishes.';

function _megabytes(bytes: number): string {
	return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function _fetching(progress: StemFetchProgress | null): StemStatusView {
	if (progress === null || progress.files_total <= 0) {
		return {
			name: 'fetching',
			label: 'FETCHING STEMS',
			tip: `Fetching this track's stems from the cloud. ${CHIPS_ARRIVE}`,
			action: null,
			busy: true
		};
	}
	const done = Math.min(progress.files_done, progress.files_total);
	return {
		name: 'fetching',
		label: `FETCHING STEMS ${done}/${progress.files_total}`,
		tip:
			`Fetching this track's stems from the cloud: ${done} of ${progress.files_total} files, ` +
			`${_megabytes(progress.bytes_done)} so far. ${CHIPS_ARRIVE}`,
		action: null,
		busy: true
	};
}

function _loading(stems: StemDeckState): StemStatusView {
	const load = stems.load ?? { phase: 'probing' as const, progress: null, reason: null };
	if (load.phase === 'probing') {
		return {
			name: 'probing',
			label: 'CHECKING STEMS',
			tip: `Checking whether this track has stems. ${CHIPS_ARRIVE}`,
			action: null,
			busy: true
		};
	} else if (load.phase === 'fetching') {
		return _fetching(load.progress);
	} else if (load.phase === 'downloading') {
		return {
			name: 'downloading',
			label: 'LOADING STEMS',
			tip: `Reading the stem files from this machine. ${CHIPS_ARRIVE}`,
			action: null,
			busy: true
		};
	} else if (load.phase === 'decoding') {
		return {
			name: 'decoding',
			label: 'DECODING STEMS',
			tip: `Decoding the stem audio. ${CHIPS_ARRIVE}`,
			action: null,
			busy: true
		};
	} else if (load.phase === 'waiting') {
		return {
			name: 'waiting',
			label: 'STEMS WAITING - LOAD NOW',
			tip:
				`Stems are held back: ${load.reason ?? 'the deck is busy'}. ` +
				'Click to load them now.',
			action: 'load_now',
			busy: false
		};
	} else if (load.phase === 'switching') {
		return {
			name: 'switching',
			label: 'SWITCHING TO STEMS',
			tip: 'The stems are decoded and are taking over the playing deck without stopping it.',
			action: null,
			busy: true
		};
	}
	const _exhaustive: never = load.phase;
	throw new Error(`Unhandled stem load phase: ${String(_exhaustive)}`);
}

export function stemStatusView(stems: StemDeckState): StemStatusView {
	if (stems.status === 'ready') {
		return {
			name: 'ready',
			label: '',
			tip:
				`real ${stems.model ?? 'Demucs'} stems (${stems.layout ?? 'demucs4'})` +
				' - click mute, Shift+click solo',
			action: null,
			busy: false
		};
	} else if (stems.status === 'loading') {
		return _loading(stems);
	} else if (stems.status === 'error') {
		return {
			name: 'error',
			label: 'STEMS FAILED - RETRY',
			tip: `Stems failed to load: ${stems.error ?? 'unknown error'}. Click to retry.`,
			action: 'retry',
			busy: false
		};
	} else if (stems.status === 'unavailable') {
		if (stems.error === null) {
			return {
				name: 'empty',
				label: '',
				tip: 'Stem buttons switch on when a track with stems is loaded on this deck.',
				action: null,
				busy: false
			};
		} else if (stems.error.startsWith(STEMS_DISABLED_PREFIX)) {
			return {
				name: 'off',
				label: 'STEMS OFF',
				tip: `Stems are switched off: ${stems.error.slice(STEMS_DISABLED_PREFIX.length)}.`,
				action: null,
				busy: false
			};
		}
		return {
			name: 'none',
			label: 'NO STEMS',
			tip:
				'This track has no stems: none on this machine and none in the cloud. ' +
				`(${stems.error})`,
			action: null,
			busy: false
		};
	}
	const _exhaustive: never = stems.status;
	throw new Error(`Unhandled stem status: ${String(_exhaustive)}`);
}
