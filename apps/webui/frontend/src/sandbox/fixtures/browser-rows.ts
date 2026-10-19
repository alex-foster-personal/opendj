/**
 * BrowserRow fixtures for TrackTable contracts.
 *
 * BrowserRow is the CLIENT row shape (pane-contract.svelte.ts), built by
 * BrowserPanel from listing payloads, so these are typed against it directly
 * rather than recorded off the wire. Defaults describe an ordinary present,
 * analyzed local track; a story overrides only the fields its contract is
 * about, so the contract reads as "this row, except X".
 *
 * Sandbox only: app code never imports src/sandbox/.
 */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

function browserRow(n: number, over: Partial<BrowserRow> = {}): BrowserRow {
	return {
		stable_id: `fixture-${n}`,
		item_id: null,
		order: n,
		title: `Track ${n}`,
		artist: `Artist ${n}`,
		key: '8A',
		bpm: 120 + n,
		rating: n % 6,
		energy: (n % 9) + 1,
		energy_source: 'mik',
		energy_reason: '',
		etag: '',
		comments: null,
		duration_ms: 300_000,
		genre: 'House',
		genre_reason: null,
		file_exists: true,
		file_availability: 'present',
		quality: null,
		play_count: n,
		is_streaming: false,
		strip: null,
		vocals: { status: 'not_analyzed' },
		stems: null,
		has_rb_mapping: false,
		artwork_available: false,
		artwork_status: 'no_image_path',
		rb_meta: null,
		revealed: true,
		match_context: null,
		lyrics: null,
		is_remix: false,
		is_radio_edit: false,
		key_status: 'ok',
		key_reason: null,
		bpm_status: 'ok',
		bpm_reason: null,
		loudness_status: 'ok',
		loudness_reason: null,
		...over
	};
}

export function browserRows(count: number): BrowserRow[] {
	return Array.from({ length: count }, (_, i) => browserRow(i + 1));
}
