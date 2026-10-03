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
