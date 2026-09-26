import type { KaraokeTrack } from '$lib/api-karaoke';
import type { TrackLyrics } from '$lib/rb/api-rb';

/** Build a karaoke-shaped payload from cached LRCLIB line text when words are absent. */
export function karaokeTrackFromLineLyrics(lineLyrics: TrackLyrics): KaraokeTrack | null {
	if (lineLyrics.lines.length === 0) return null;
	const words: KaraokeTrack['words'] = [];
	const lines: NonNullable<KaraokeTrack['lines']> = [];
	for (let i = 0; i < lineLyrics.lines.length; i += 1) {
		const line = lineLyrics.lines[i];
		const start_s = line.start_ms / 1000;
		const nextStartMs = lineLyrics.lines[i + 1]?.start_ms;
		const end_s =
			nextStartMs !== undefined ? nextStartMs / 1000 : start_s + Math.max(0.25, line.text.length * 0.08);
		const idx = i;
		words.push({
			idx,
			word: line.text,
			start_s,
			end_s,
			score: null,
			line_final: true
		});
		lines.push({
			text: line.text,
			first_idx: idx,
			last_idx: idx,
			band: 'unjudged',
			quality: null,
			n_red: 0,
			n_judged: 0,
			n_words: 1,
			para_final: false,
			start_s,
			end_s
		});
	}
	return {
		words,
		lines,
		verdict: {
			effective: 'vocal',
			effective_verdict: 'vocal',
			override: null,
			language_iso3: null,
			has_words: true,
			computed_at: '1970-01-01T00:00:00Z',
			stable_id: lineLyrics.stable_id,
			verdict: 'vocal',
			artist: null,
			coverage_pct: null,
			n_lines: lines.length,
			n_words: words.length,
			title: null,
			pipeline_version: 'line-cache-fallback',
			updated_at: '1970-01-01T00:00:00Z'
		}
	} satisfies KaraokeTrack;
}
