/**
 * BACKSTAGE-08: pure logic behind the Backstage "Human review" widget at
 * /admin?p=triage. Kept free of Svelte and of the network so the key map and
 * the queue stepping are unit-tested directly.
 *
 * The queue is the REAL triage listing (GET /api/v1/lyrics?order=suspect),
 * one item per track. The line shown for an item is that track's most
 * suspect line: the line with the most witness-red words in
 * GET /tracks/{id}/lyrics/words?include=lines.
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { navArrowKey } from '$lib/shell/backstage-nav';

export type ReviewLabel = 'correct' | 'wrong-words' | 'wrong-timing' | 'not-this-song';

/** Key, label id and button text, in key order. */
export const REVIEW_LABELS: readonly {
	key: '1' | '2' | '3' | '4';
	id: ReviewLabel;
	text: string;
}[] = [
	{ key: '1', id: 'correct', text: 'Correct' },
	{ key: '2', id: 'wrong-words', text: 'Wrong words' },
	{ key: '3', id: 'wrong-timing', text: 'Wrong timing' },
	{ key: '4', id: 'not-this-song', text: 'Not this song' }
];

/**
 * There is no write endpoint for these four labels today: the only lyric
 * write is PUT /tracks/{id}/lyrics/override, whose values (vocal, sparse,
 * no-lyrics) are a different question. So the labels render inert.
 */
export const LABELS_WRITABLE = false;

export type ReviewAction =
	| { kind: 'label'; label: ReviewLabel }
	| { kind: 'skip' }
	| {
			kind: 'step';
			arrow: 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight';
			modified: boolean;
	  };

export interface ReviewKeyEvent {
	key: string;
	shiftKey?: boolean;
	ctrlKey?: boolean;
	altKey?: boolean;
	metaKey?: boolean;
}

/**
 * Map one keydown to a review action, or null to leave the key alone.
 * 1-4 label, 0 skips (both only unmodified, so Ctrl/Cmd+1 stay the
 * browser's). W/A/S/D are the arrow keys exactly, modifiers included: Up/Left
 * (W/A) step back, Down/Right (S/D) step forward, any modifier jumps to the
 * first/last item.
 */
export function reviewKeyAction(
	event: ReviewKeyEvent,
	target: unknown = null
): ReviewAction | null {
	if (isTextEntryTarget(target as EventTarget | null)) return null;
	const modified = Boolean(event.shiftKey || event.ctrlKey || event.altKey || event.metaKey);
	const arrow = navArrowKey(event.key);
	if (arrow !== null) return { kind: 'step', arrow, modified };
	if (modified) return null;
	if (event.key === '0') return { kind: 'skip' };
	const label = REVIEW_LABELS.find((l) => l.key === event.key);
	return label ? { kind: 'label', label: label.id } : null;
}

/** Index after a skip (or a label): the next item, held at the last one. */
export function advanceReview(current: number, count: number): number {
	if (count <= 0) return -1;
	return Math.min(count - 1, Math.max(0, current + 1));
}

/** "n of N", 1-based; "0 of 0" for an empty queue. */
export function positionText(index: number, count: number): string {
	if (count <= 0 || index < 0) return `0 of ${Math.max(0, count)}`;
	return `${index + 1} of ${count}`;
}

export interface ReviewWord {
	word: string;
	start_s?: number | null;
	end_s?: number | null;
}
export interface ReviewLine {
	first_idx: number;
	last_idx: number;
	n_red: number;
	end_s: number | null;
}
export interface SuspectLine {
	text: string;
	start_s: number | null;
	end_s: number | null;
	n_red: number;
	line_no: number;
}

/** The line with the most witness-red words; the earliest wins a tie. */
export function pickSuspectLine(
	words: ReviewWord[],
	lines: ReviewLine[] | null | undefined
): SuspectLine | null {
	if (!lines || lines.length === 0) return null;
	let best = 0;
	for (let i = 1; i < lines.length; i++) if (lines[i].n_red > lines[best].n_red) best = i;
	const line = lines[best];
	const slice = words.slice(line.first_idx, line.last_idx + 1);
	const last = slice[slice.length - 1];
	return {
		text: slice.map((w) => w.word).join(' '),
		start_s: slice[0]?.start_s ?? null,
		end_s: line.end_s ?? last?.end_s ?? null,
		n_red: line.n_red,
		line_no: best + 1
	};
}

/** m:ss.s for a timing readout; "-" when unknown. */
export function fmtTime(seconds: number | null): string {
	if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '-';
	const m = Math.floor(seconds / 60);
	const s = seconds - m * 60;
	return `${m}:${s < 10 ? '0' : ''}${s.toFixed(1)}`;
}
