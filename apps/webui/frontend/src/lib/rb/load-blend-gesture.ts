/**
 * Pointer-distance state machine for the Load-to-CHn intro blend (issue #286).
 *
 * Load is committed by the UI on pointerdown. This object only owns phase:
 * idle -> down -> morph|done|aborted. Mixer writes stay in the Svelte layer.
 */
import {
	LOAD_BLEND_THRESHOLD_PX,
	loadBlendEqAt,
	loadBlendProgress,
	loadBlendScrubMs
} from '$lib/rb/load-blend-math';

type DeckId = 1 | 2 | 3 | 4;

type LoadBlendPhase = 'idle' | 'down' | 'morph' | 'done' | 'aborted';

type LoadBlendMoveResult =
	| { kind: 'ignore' }
	| { kind: 'hold' }
	| { kind: 'refuse'; reason: string }
	| { kind: 'morph'; entered: boolean };

type LoadBlendUpResult = { kind: 'ignore' } | { kind: 'click' } | { kind: 'morph' };

type LoadBlendAbortResult = { kind: 'ignore' } | { kind: 'cancel' } | { kind: 'restore' };

export function createLoadBlendSession(args: {
	incomingDeck: DeckId;
	stableId: string;
	masterDeck: DeckId | null;
}) {
	let phase: LoadBlendPhase = 'idle';
	let originX = 0;
	let originY = 0;
	let lastX = 0;
	let lastY = 0;
	let originMs = 0;
	let durationMs = 0;
	let refused = false;

	function dxPx(): number {
		return lastX - originX;
	}

	function dyPx(): number {
		return originY - lastY;
	}

	function progress(): { fader: number; t: number } {
		return loadBlendProgress({ dyPx: dyPx() });
	}

	function positionMs(): number {
		return loadBlendScrubMs({ dxPx: dxPx(), originMs, durationMs });
	}

	return {
		get phase(): LoadBlendPhase {
			return phase;
		},
		get incomingDeck(): DeckId {
			return args.incomingDeck;
		},
		get masterDeck(): DeckId | null {
			return args.masterDeck;
		},
		get stableId(): string {
			return args.stableId;
		},
		get t(): number {
			return progress().t;
		},
		get fader(): number {
			return progress().fader;
		},
		get scrubDeltaMs(): number {
			return positionMs() - originMs;
		},
		get positionMs(): number {
			return positionMs();
		},
		get eq(): ReturnType<typeof loadBlendEqAt> {
			return loadBlendEqAt(progress().t);
		},
		setScrubOrigin(ms: number): void {
			originMs = ms;
		},
		setDurationMs(ms: number): void {
			durationMs = ms;
		},
		pointerDown(x: number, y: number): void {
			if (phase !== 'idle') return;
			originX = lastX = x;
			originY = lastY = y;
			phase = 'down';
		},
		pointerMove(x: number, y: number): LoadBlendMoveResult {
			if (phase !== 'down' && phase !== 'morph') return { kind: 'ignore' };
			lastX = x;
			lastY = y;
			if (phase === 'morph') return { kind: 'morph', entered: false };
			if (refused) return { kind: 'hold' };
			const dist = Math.hypot(x - originX, y - originY);
			if (dist < LOAD_BLEND_THRESHOLD_PX) return { kind: 'hold' };
			if (args.masterDeck === null) {
				refused = true;
				return { kind: 'refuse', reason: 'no master deck' };
			}
			if (args.masterDeck === args.incomingDeck) {
				refused = true;
				return { kind: 'refuse', reason: 'incoming deck is master' };
			}
			phase = 'morph';
			return { kind: 'morph', entered: true };
		},
		pointerUp(): LoadBlendUpResult {
			if (phase === 'down') {
				phase = 'done';
				return { kind: 'click' };
			}
			if (phase === 'morph') {
				phase = 'done';
				return { kind: 'morph' };
			}
			return { kind: 'ignore' };
		},
		abort(): LoadBlendAbortResult {
			if (phase === 'down') {
				phase = 'aborted';
				return { kind: 'cancel' };
			}
			if (phase === 'morph') {
				phase = 'aborted';
				return { kind: 'restore' };
			}
			return { kind: 'ignore' };
		}
	};
}

type LoadBlendSession = ReturnType<typeof createLoadBlendSession>;
