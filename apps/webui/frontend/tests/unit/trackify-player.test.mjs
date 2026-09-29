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
