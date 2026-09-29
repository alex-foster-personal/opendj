/**
 * Space stops a sounding preview, before anything else can see the key.
 *
 * Mini-PRD (CUEOUT-15 R5, the maintainer Wed 16 Sep 2026: "space is the 'stop the
 * preview then continue as normal'"):
 * - ✔︎ R5.1 While a preview is sounding, Space stops it and is consumed: no deck
 *   transport toggle, no focused-button activation, no page scroll.
 *   - [if] a preview is playing and Space is pressed on page chrome [then ⛔️]
 *     the preview stops and the deck play toggle never runs
 *   - [if] a preview is playing and a button has focus [then ⛔️] the button is
 *     not activated (keydown AND keyup are swallowed)
 *   - [if] Space is held after stopping a preview [then ⛔️] the auto-repeat
 *     keydowns do not fall through and toggle a deck
 * - ✔︎ R5.2 Once released, Space is exactly what it was before.
 *   - [if] no preview is playing [then ⛔️] Space is not touched at all
 *   - [if] Space was released after a stop [then ⛔️] the next press reaches the
 *     deck transport as normal
 * - ✔︎ R5.3 Typing is never interrupted.
 *   - [if] a text field has focus [then ⛔️] Space types a space and the preview
 *     keeps playing
 *
 * Top priority comes from a CAPTURE-phase listener on window plus
 * stopImmediatePropagation, so it runs before any focused element's own
 * handler. `performance-hotkeys.ts` is ALSO a window capture listener (since
 * #4009), so the two are ordered only by registration: the performance page
 * must install this one first (routes/performance/+page.svelte). The
 * decision logic takes its dependencies as arguments, so it is testable
 * without the preview engine, an AudioContext or a DOM.
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export interface PreviewSpaceStopDeps {
	isPreviewPlaying: () => boolean;
	stopPreview: () => void;
}

export interface PreviewSpaceStopKeyEvent {
	readonly code: string;
	readonly key: string;
	readonly target: EventTarget | null;
	preventDefault(): void;
	stopImmediatePropagation(): void;
}

const _isSpace = (e: PreviewSpaceStopKeyEvent): boolean => {
	return e.code === 'Space' || e.key === ' ' || e.key === 'Spacebar';
};

export { isTextEntryTarget };

export function createPreviewSpaceStop(deps: PreviewSpaceStopDeps): {
	onKeyDown: (e: PreviewSpaceStopKeyEvent) => void;
	onKeyUp: (e: PreviewSpaceStopKeyEvent) => void;
} {
	// Set from the keydown that stopped a preview until that key is released,
	// so a held Space cannot auto-repeat through into the deck transport.
	let swallowingUntilRelease = false;

	const consume = (e: PreviewSpaceStopKeyEvent): void => {
		e.preventDefault();
		e.stopImmediatePropagation();
	};

	return {
		onKeyDown(e) {
			if (!_isSpace(e)) return;
			if (swallowingUntilRelease) {
				consume(e);
				return;
			}
			if (!deps.isPreviewPlaying() || isTextEntryTarget(e.target)) return;
			swallowingUntilRelease = true;
			consume(e);
			deps.stopPreview();
		},
		onKeyUp(e) {
			if (!_isSpace(e) || !swallowingUntilRelease) return;
			swallowingUntilRelease = false;
			consume(e);
		}
	};
}

/** Install on window in the capture phase. Returns the teardown. */
export function installPreviewSpaceStop(deps: PreviewSpaceStopDeps): () => void {
	const handler = createPreviewSpaceStop(deps);
	window.addEventListener('keydown', handler.onKeyDown, { capture: true });
	window.addEventListener('keyup', handler.onKeyUp, { capture: true });
	return () => {
		window.removeEventListener('keydown', handler.onKeyDown, { capture: true });
		window.removeEventListener('keyup', handler.onKeyUp, { capture: true });
	};
}
