import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

describe('TrackifyPlayer component contract', () => {
	it('renders a position bar without waveform canvas or mixer imports', () => {
		const playerSource = readFileSync(
			new URL('../../src/lib/components/rb/TrackifyPlayer.svelte', import.meta.url),
			'utf8'
		);
		assert.doesNotMatch(playerSource, /WaveformStack|Mixer|canvas/i);
		assert.match(playerSource, /PositionBar/);
		assert.match(playerSource, /data-testid="trackify-player"/);

		const pageSource = readFileSync(
			new URL('../../src/routes/music-player/+page.svelte', import.meta.url),
			'utf8'
		);
		assert.match(pageSource, /TrackifyPlayer/);

		const barSource = readFileSync(
			new URL('../../src/lib/components/rb/PositionBar.svelte', import.meta.url),
			'utf8'
		);
		assert.match(barSource, /data-testid="trackify-position-bar"/);
		assert.doesNotMatch(barSource, /<canvas/i);
	});
});

describe('TrackifyPlayer with no track loaded', () => {
	// if Play is clickable with nothing loaded, the press raises a red "Deck audio hit a limit" toast -- broken.
	const source = readFileSync(
		new URL('../../src/lib/components/rb/TrackifyPlayer.svelte', import.meta.url),
		'utf8'
	);

	it('disables Play and says why', () => {
		const play = source.slice(source.indexOf('<button'), source.indexOf('</button>'));
		assert.match(play, /togglePlay\(\)/, 'the first transport button is Play');
		assert.match(play, /disabled=\{!loaded\}/);
		assert.match(play, /title=\{playTitle\}/);
		assert.match(source, /!loaded \? 'no track loaded - nothing to play'/);
	});

	it('derives loaded from the deck having a track', () => {
		assert.match(source, /const loaded = \$derived\(deck\.stable_id !== null\);/);
	});
});

describe('TrackifyPlayer now-playing view (PERFMODE-6635)', () => {
	const source = readFileSync(
		new URL('../../src/lib/components/rb/TrackifyPlayer.svelte', import.meta.url),
		'utf8'
	);

	it('draws in the Gig palette, not unstyled defaults', () => {
		assert.match(source, /import '\$lib\/rb\/theme\.css';/);
		assert.match(source, /class="perf-root trackify-player"/);
		// the old look: a bare 40rem column with no palette at all
		assert.doesNotMatch(source, /max-width: 40rem/);
	});

	it('shows time as minutes:seconds, never a raw ms count, each with a hover title', () => {
		assert.match(source, /formatMsAsClock\(positionMs\)/);
		assert.match(source, /formatMsAsClock\(deck\.duration_ms\)/);
		assert.doesNotMatch(source, /\} ms\b/);
		assert.match(source, /title="Playback position \(minutes:seconds\)"/);
		assert.match(source, /title="Track duration \(minutes:seconds\)"/);
	});

	it('shows real artwork or the gray slate, never a fabricated cover', () => {
		assert.match(source, /artworkUrl\(deck\.stable_id, 'm'\)/);
		assert.match(source, /onerror=\{\(\) => \(artFailed = true\)\}/);
		assert.match(source, /data-testid="trackify-artwork-slate"/);
	});

	it('titles the BPM and key chips', () => {
		assert.match(source, /title="Track tempo in beats per minute, from the library"/);
		assert.match(source, /title="Track key in Camelot notation, from the library"/);
	});
});
