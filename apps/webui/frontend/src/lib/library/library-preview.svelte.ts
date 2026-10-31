/**
 * LIBWS-04 / LIBWS-06: the Crate Desk's audio and deck hand-off.
 *
 * Preview plays through library-preview-player.ts (its own short-lived
 * AudioContext, never the Gig engine). A load to deck N leaves Library mode
 * for Gig with the track in the deck's deep-link slot (`/performance?dN=`),
 * which the Gig route's session restore loads on arrival: Library mode itself
 * still builds no deck.
 */

import { libraryAudioLoadRefusal } from '$lib/components/rb/browser/browser-row-wire';
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';
import { fetchAudioArrayBuffer } from '$lib/rb/api-rb';
import { pushToast } from '$lib/stores.svelte';
import {
	createLibraryPreviewPlayer,
	type LibraryPreviewPlayer,
	type LibraryPreviewSnapshot,
	type PreviewContext
} from './library-preview-player';

type DeckId = 1 | 2 | 3 | 4;

/** The row facts a refusal needs: BrowserPanel's LoadableRow, the same ones a Gig deck load reads. */
export type LibraryAudioRow = Pick<BrowserRow, 'stable_id' | 'file_exists' | 'is_streaming'> & {
	rb_meta?: BrowserRow['rb_meta'];
	file_availability?: BrowserRow['file_availability'];
	file_path?: string | null;
};

export const libraryPreview: LibraryPreviewSnapshot = $state({
	stable_id: null,
	status: 'idle',
	position_ms: 0,
	duration_ms: 0,
	error: null
});

const player = createLibraryPreviewPlayer({
	fetchAudio: fetchAudioArrayBuffer,
	createContext: () => new AudioContext({ latencyHint: 'playback' }),
	onchange: (next) => {
		const failed = next.status === 'error' && libraryPreview.status !== 'error';
		Object.assign(libraryPreview, next);
		if (failed && next.error !== null) pushToast(next.error, 'error');
	},
	setInterval: (fn, ms) => setInterval(fn, ms),
	clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
	setTimeout: (fn, ms) => setTimeout(fn, ms),
	clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>)
});

/** Null when the row has local audio, else the reason (toasted by callers). */
export function libraryAudioRefusal(row: LibraryAudioRow): string | null {
	const refusal = libraryAudioLoadRefusal(row);
	return refusal === null ? null : refusal.replace(/^cannot load: /, '');
}

/** Play `row` from `ratio` (a strip click, or 0 for a double-click). */
export function previewLibraryRow(row: LibraryAudioRow, ratio = 0): void {
	const refusal = libraryAudioRefusal(row);
	if (refusal !== null) {
		pushToast(`preview: ${refusal}`, 'error');
		return;
	}
	void player.play(row.stable_id, ratio);
}

/** The preview bar's Play/Pause button. */
export function toggleLibraryPreview(row: LibraryAudioRow): void {
	const refusal = libraryAudioRefusal(row);
	if (refusal !== null) {
		pushToast(`preview: ${refusal}`, 'error');
		return;
	}
	void player.toggle(row.stable_id);
}

export function stopLibraryPreview(): void {
	player.stop();
}

/** The same player the bar drives, for the agent IPC (library-preview-ipc.ts). */
export const libraryPreviewPlayer: LibraryPreviewPlayer = player;

/** The Gig route with `stable_id` in deck `deck`'s deep-link slot. */
export function gigDeckLoadHref(stable_id: string, deck: DeckId): string {
	const params = new URLSearchParams();
	params.set(`d${deck}`, stable_id);
	return `/performance?${params.toString()}`;
}
