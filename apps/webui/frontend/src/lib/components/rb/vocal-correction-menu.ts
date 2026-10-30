/**
 * QuickDraw contextual item for vocal-area Fix / add comment (FB-12).
 *
 * Reads the surface's bounding rect (DOM) and hands a PinDraft to the
 * feedback store. Hit-test math lives in vocal-correction.ts.
 */
import { resolveDisplayedAnlz } from '$lib/components/rb/wave/anlz-cache.svelte';
import { vocalsOf } from '$lib/rb/api-rb';
import { getDeckState } from '$lib/rb/audio-engine.svelte';
import { pinFromClient } from '$lib/rb/feedback';
import { feedbackState, queuePinDraft } from '$lib/rb/feedback-store.svelte';
import {
	encodeVocalAnchor,
	formatVocalComment,
	hitFromVocals,
	parseVocalAnchor,
	type VocalsLike
} from '$lib/rb/vocal-correction';
import { waveClickAt } from './wave-click';

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

export function vocalFixMenuItem(
	event: MouseEvent,
	target: EventTarget | null
): VocalFixMenuItem | null {
	const click = waveClickAt(event, target);
	if (click === null) return null;
	const deck = getDeckState(click.deck);
	const timeS = click.timeS;
	let vocals: VocalsLike;
	try {
		const anlz = resolveDisplayedAnlz(deck.anlz, click.stableId);
		vocals = anlz === null ? { status: 'not_analyzed' } : vocalsOf(anlz);
	} catch {
		return null;
	}

	const hit = hitFromVocals({ stableId: click.stableId, vocals, timeS });
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
