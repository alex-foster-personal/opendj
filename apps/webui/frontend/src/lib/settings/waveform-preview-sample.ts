/**
 * The Settings waveform preview's sample: a synthetic tri-band detail slice from
 * tests/unit/fixtures/blocks-variance-anlz-window.json, so the thumbnail shows
 * what each design x palette looks like on music (the maintainer, Tue 6 Oct 2026).
 *
 * Source: seconds 8..20 of the 24 s deck-row window in
 * tests/unit/fixtures/blocks-variance-anlz-window.json, each band downsampled by
 * per-bucket max over 2 points (as the server downsamples detail). 12 s
 * rather than 24 s so a card-wide preview shows single beats the way a deck row does.
 * Stored as PWV bytes (0..127, value = byte / 127), base64.
 * Regenerate from that fixture via scripts/write-deck-window-derived.mjs, never hand-edit.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';

const BANDS = {
	low: 'NDAvLS4sKSsqKiosKisrKCsqKyoqKyonKiooKR0eHR0cJSUlISAhHSAcGx8eHhsfHB8aGxwdHh0bHRweHBweHRscGx4oJSUkHyEgHiAgHRweHx4cHBweHh4cGh0dHB0eGxsdHB4dHCUjIyEhIiEdIBwdHx0dHh4fGh4eHR0dHh4eHh0dHB0cHhweHickIiMgICAgHx0fHx0eGxwcHxscHh0fHB4eGx0dHRscHh0eJyYlICEhHhweHh8bHR4dHRseHRsbHB4eHR4eHRseHB4cHB4mJSMiICIgHh8fHx4eHh8dGx4eHhweHB4cHhoeHh4bGh4dHiUmISEgICEeHB8eHR4fHh4cHB0eHR4dHh4cHh0dHR4eHB0eJSclIyIiHyAeHB4cHR4eHB4bHB0dHR0eHhscHR4cHh0bHB4cJCQiICEhIB4fHh4fHjAzMzIwMjIyMTAwMjIyLzIzMzIxMTJDPjs6OTc1NjMzMzIwMjAwMjIwLzMvMDMyMjAwLzIvMi8zMkNAOjw4ODc2NTEyMjMzMTIzMzIyMTAxMjAxMDAzLzEvMjMvQD87PDo2NzU1NDQ0MTMzMjMxLzEyMTMxMDEwMDIzMTIyMTJCQT08NzY0MzQxNTA0MzIyMTEzMjExMjEyMzIwMjExMTEyMjE/Pjw4NjU3MjQzNDAwMzIxMTMzMjIzMzIvMjIzMDMwMzIyL0JAPDg4NjYzMzMyMzIxMTEwMzEwMjEyMDMxMDAyMjIxMDEzQD4+ODk4NTYyNTQzMjAwMTMwMTEyMzMyMjIxMTExMzMuMi5DQTo5ODY3NjYzNDEzMzMwMjMyMzIxMi8yMDIxMzIvMjIVFBsbGBoWGRcYFxcWFRMWFhYVFBYVFxcXFhQUFRIWFxYWFRYWFRwcGRkZFhUWFRYXFhUWFxcUFhYTFBYVFRYXFhcUFhcTExYUGhwYFxYYFxQXFhUXFxQUFRYVFhQWFBcXFxYTFRMWFBYVFhMbHBkYFxYWFxYYFBYUFBcXFhQWFBQTFhYXEhYWFxUVExcSFx0bGhkZFxgWFRYVFxcVFhYVFhUWExUWFxMTFhcWFRYXFhYWGhwaGhcZFRYXExYWFBcWEhcVFxcVFRQVFBcVFhQWFRMVFhYWGRwaGhcWGBYYFhUXFhYWFBYWFhUXExMXFhMXFxUVFRcVFhcbHRcXFxgYGBcWFhYWFhcTFBUUFxUUFg==',
	mid: 'HhwdGx0cGRwbGhsdGxwcRU5PTEQ+OjUtLSolJBgXFhUTEQ8QDg4QDA8MCw8NDgsPDA4KCgwMDQwKDAsNCwsMDAkLCgwNCw0NCgwMCw0NCwoMDQ0LCwwuMDEqJSIiHh0bFhUWFRYUExINDw4QEhIPEg8QExERExMUEBMUEhMTFBUVFRQUExMTFRMVFRUUFBQTFBUUFBMUFBMTEBIRMzM2Mi4qJiMiGxwbGRYVFhUVEBERDQ8QDQsODQ4LDQ4NDQsODQoKDA0NDA0NCwoMCg0LCwwLCwsLCg0NCwwNDQ0NDQ4NCw0wMy0rJiMgHxkbGRkVExYVFRARDQ8PEBIPDxIREBITEhMREhMUExQUFBQTFBQTFBQVExQVExUVFRUVExUTERMSEhMTEhQRMjY2MC0pJiEfHR0aGRgUFBUSDQ4ODQ8QDw0PDQ4PDhQXFxUUFhUVFBMTFRUVERQVFRQTExQTEhETFBQTFRITFBMSFBITFkpPUVBFQD03My0pJycjJSEjIhsbGR0cHR4eHhscHB4fHh8gISAgIB4gIR8hIB8jHyEfIiMgIyAgIyMhIyEiISIiHyEiICEfVV5aU0tFPTo1MS8sKSglIyMiGxkaFxgXFhgVGRQYFxYVFRUXFhQUFRQUFRUSFBMTExMUFBMTFBMSEhIVERMTFRISFhUUFUtRVk5HPzkzMy8sKCgkJSMiHxwbHBsdHB0bHB0dHh4dHh4dIB8fICAhICIgHx8iIiIiISEjICAjHyIjISMfIiIhIB4eHyEeWV1XUUtBPTozMS4rKykiJB8hGxcZGBgZGRkXGBYXFxcUFhcVFhUUFRIVEhUTFRQRFBQICAgIBgkGCgkKCQkICAcJCgoJCCIjJSAdGxcVFBETExIRDxAPDgwNDAwNCwoMCw0ODQ0ODw8NDw8MDQ8NDg8QDxAODxAMDBANDQ8NDQ0PDgwPDg4PDwwMDg8hJycmHx8dHBgVFRITEREQEAwLDAsKCgkKCgoMCAoJCAsLCgkKCAgHCgoKBgkKCgkJBgoGCgkJCQkJCAkIBwkICgoJCQoJCiAlICAcHBYUFRUTERESEBAPDwwLDQsNCgsNCg0NCw4NCg8NDw8NDgwNDRAODw0QDgwPDxAPDBAPEA0ODw4QDg0PDg8ODA8lKCgmHhwcGhYYFRMSERIQEBAKDQgJCgsLDAsKCgoKCgsICAkICwkICQ==',
	high: 'CQgJCAkJBwkICAgJCAkJIBsVEQ4MCwoICQkICQYHBgYGBgYHBgYHBQYFBQcGBgUHBgcFFA8MCggHBwYHBgYHBgUGBQcHBgcHBQYGBgcHBgUGBwYFBQYXEQ0JBwcHBgcHBQUGBgcGBgUFBgUGBwcFBwUGBwUGBgYHBRQSDAsICAgHBwYGBgYGBgYHBwYGBgYGBgcHBgYHBwYGBQUFFhENCwkJBwcHBQYGBgUGBwYHBgcHBQYHBgUGBgYFBgcGBgUXFA0KCQkIBwcHBgUHBQcGBgYGBgYGBQcHBgYHBwYGBwcGBQYUDgsKCAgHBwUHBwcFBQYGBwUGBAYFBgcFBQYGBQYHBgYFBhURDAsICAcGBwYGBgYHBgYHBgcHBwcHBgcGBQYFBgYGBQcFFhENCgkICAYGBgcGBgYFBgYFBQYFBQYHBwYGBgYHBgkLCwojHhgSDw0LDAwLCQsLCwsKCgoKCgkKCwsKCwoKCgoJCgkJCicgFxQPDQ0MCwoKCQsJCwkLCwoKCQsKCwsLCwkJCgoLCgoLCyIaFBAODQsLCgoLCQoJCwsJCwkJCwsKCwoLCgsLCgsLCgsKJBsUEQ8NCwsKCgsLCgsKCgoLCwoKCQoJCQoJCwgLCgoKCgomHBYSDw0MDAsKCwoKCgoKCwoKCwoJCgkLCQoKCwkJCwoKCiQgGBMQDgwKDAsLCgsKCwsKCQsLCwoLCgoJCQoKCgoJCgoJJyAXEw8ODAwLCgoLCgoKCgoLCQoLCQoLCgsJCwsKCgkJCgsJIRkTEQ8MDAsKCgoKCwsJCgkLCwkKCgoLCwsKCwoLCwsJCgskGhYRDgwNCwsKCwsJCwsFBAQEAwUDBQQFBAUEBAMFBQUEBBELCggHBgUFBQMFBQUFBAUFBAUFBQUFBAMEBAQFBQQFBQUEEA4JBwcFBQYGBQUEBQUDAwUEBAUEBAQFBAMFBAQFBQQEBQUPDQoJBgcGBgUEBQQFBAUEBQMEBQQEBAQEBAQFBAUEBAUFBQQOCggGBwYGAwUFBQUFAwUDBQUFBQUFBAUEBAUEBQUEBQUEBRALCAcGBgQEBQUFBAUFBQUFBQQEBQQFBAQFAwUEAwUFAwUEEQ0JCAYFBQYFBQQFBAMFBQUFAwUFBQQEBQQFBQQFBQUFBAUQDAoJBQUGBQQFBQQEBAUEBQUEBQMEBAUFBQUEBAUEBQUDBBAMCggGBg=='
} as const;

/** Points per band (901). */
export const PREVIEW_SAMPLE_POINTS = 901;

/** Seconds of music the sample spans (shared with the preview beat grid). */
export { PREVIEW_SAMPLE_SECONDS } from './waveform-preview-beats';

function _decode(b64: string): number[] {
	const raw = atob(b64);
	const out = new Array<number>(raw.length);
	for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i) / 127;
	return out;
}

export function previewSampleBands(): { low: number[]; mid: number[]; high: number[] } {
	return { low: _decode(BANDS.low), mid: _decode(BANDS.mid), high: _decode(BANDS.high) };
}

/** The preview's waveform object. Built once per page: the deck painter caches
 * its per-waveform norms by identity, so repaints reuse them. */
let _sample: AnlzWaveform | undefined;

export function previewSampleWaveform(): AnlzWaveform {
	if (_sample === undefined) {
		const detail = { length: PREVIEW_SAMPLE_POINTS, ...previewSampleBands() };
		_sample = { kind: 'tri', preview: detail, detail };
	}
	return _sample;
}
