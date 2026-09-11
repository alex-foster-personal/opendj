import assert from 'node:assert/strict';
import test from 'node:test';

import { assertMirrorInvariants, runLiveTier } from './ui-mirror-invariants.mjs';

test('if no app is open then the tier skips cleanly rather than failing or hanging', async () => {
	const result = await runLiveTier({ lockCandidates: [] });
	assert.equal(result.status, 'skipped');
});

test('if a deck is playing and master RMS reads 0 across 2 s then the tier fails', () => {
	assert.throws(() => assertMirrorInvariants(
		{
			context_state: 'running',
			decks: { '1': { playing: true, position: { ms: 100 } } },
			master: { rms: 0 },
			xrun_sentinel: { callbacks: 1000, xruns: 0 }
		},
		{
			context_state: 'running',
			decks: { '1': { playing: true, position: { ms: 2100 } } },
			master: { rms: 0 },
			xrun_sentinel: { callbacks: 2000, xruns: 0 }
		}
	));
});
