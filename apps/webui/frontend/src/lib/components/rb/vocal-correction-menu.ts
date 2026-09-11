/**
 * QuickDraw contextual item for vocal-area Fix / add comment (FB-12).
 *
 * Reads the surface's bounding rect (DOM) and hands a PinDraft to the
 * feedback store. Hit-test math lives in vocal-correction.ts.
 */
import { resolveDisplayedAnlz } from '$lib/components/rb/wave/anlz-cache.svelte';
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import { vocalsOf } from '$lib/rb/api-rb';
import { getDeckState } from '$lib/rb/audio-engine.svelte';
import { pinFromClient } from '$lib/rb/feedback';
import { feedbackState, queuePinDraft } from '$lib/rb/feedback-store.svelte';
import {
	encodeVocalAnchor,
	formatVocalComment,
	hitFromVocals,
	parseVocalAnchor,
	stripTimeS,
	waveRowTimeS,
	type VocalsLike
} from '$lib/rb/vocal-correction';

export const VOCAL_FIX_ITEM_ID = 'vocal-fix-comment';
export const VOCAL_FIX_TEST_ID = 'quick-draw-vocal-fix';
export const VOCAL_FIX_LABEL = 'Fix / add comment';

const COMMENT_UNAVAILABLE =
	'Comment pins - this daemon does not serve /api/v1/feedback, so dropping a pin is unavailable';
const COMMENT_PROBING = 'Comment pins - probing the daemon for /api/v1/feedback';

export type VocalFixMenuItem = {
	id: typeof VOCAL_FIX_ITEM_ID;
	label: typeof VOCAL_FIX_LABEL;
	testId: typeof VOCAL_FIX_TEST_ID;
	disabled: boolean;
	title?: string;
	run: (pressT0Ms?: number) => Promise<void>;
};

function _deckFromTarget(t: EventTarget | null): DeckId | null {
	const el = t instanceof Element ? t.closest('[data-deck]') : null;
	if (el === null) return null;
	const n = Number(el.getAttribute('data-deck'));
	return DECK_IDS.includes(n as DeckId) ? (n as DeckId) : null;
}

export function vocalFixMenuItem(
	event: MouseEvent,
	target: EventTarget | null
): VocalFixMenuItem | null {
	const el = target instanceof Element ? target : null;
	if (el === null) return null;
	const surface = el.closest('[data-wave-surface="row"], [data-wave-surface="strip"]');
	if (!(surface instanceof HTMLElement)) return null;
	const kind = surface.getAttribute('data-wave-surface');
	const deckId = _deckFromTarget(surface);
	if (deckId === null) return null;
	const deck = getDeckState(deckId);
	if (deck.stable_id === null) return null;
	if (deck.duration_ms === null || deck.duration_ms <= 0) return null;

	const rect = surface.getBoundingClientRect();
	if (rect.width <= 0) return null;
	const pointerX = event.clientX - rect.left;
	const timeS =
		kind === 'row'
			? waveRowTimeS({
					pointerX,
					widthPx: rect.width,
					centerPositionMs: deck.position_ms,
					durationMs: deck.duration_ms,
					pitch: deck.pitch
				})
			: stripTimeS({
					pointerX,
					widthPx: rect.width,
					durationMs: deck.duration_ms
				});

	let vocals: VocalsLike;
	try {
		const anlz = resolveDisplayedAnlz(deck.anlz, deck.stable_id);
		vocals = anlz === null ? { status: 'not_analyzed' } : vocalsOf(anlz);
	} catch {
		return null;
	}

	const hit = hitFromVocals({ stableId: deck.stable_id, vocals, timeS });
	const anchor = encodeVocalAnchor(hit);
	if (parseVocalAnchor(anchor) === null) return null;

	const availability = feedbackState.availability;
	const disabled = availability !== 'ok';
	const item: VocalFixMenuItem = {
		id: VOCAL_FIX_ITEM_ID,
		label: VOCAL_FIX_LABEL,
		testId: VOCAL_FIX_TEST_ID,
		disabled,
		run: async () => {
			queuePinDraft({
				point: pinFromClient(
					event.clientX,
					event.clientY,
					window.innerWidth,
					window.innerHeight
				),
				anchor,
				text: formatVocalComment(hit),
				page: location.pathname,
				viewport: { width: window.innerWidth, height: window.innerHeight }
			});
		}
	};
	if (availability === 'missing') item.title = COMMENT_UNAVAILABLE;
	else if (availability === 'unknown') item.title = COMMENT_PROBING;
	return item;
}
