/**
 * Pure helpers lifted out of WaveRow.svelte and routes/performance/+page.svelte
 * (quality ratchet, PR #3645) keep the exact behavior they had inline.
 *
 * - if the vocals tooltip drifts then a barless vocal state reads as ambiguous
 * - if tech-mode passthrough is not undone then another route inherits a
 *   transparent body
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let vocals;
let passthrough;

before(async () => {
	vocals = await loadTypeScriptModule('src/lib/components/rb/wave/vocals-title.ts');
	passthrough = await loadTypeScriptModule('src/lib/rb/desktop-passthrough.ts');
});

function anlzWithVocals(v) {
	const emptyBands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: 'abc123',
		points: 0,
		waveform: { kind: 'mono', preview: emptyBands, detail: emptyBands },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals: v
	};
}

test('wave row vocals title names every barless state and stays quiet for rekordbox bars', () => {
	assert.equal(vocals.waveRowVocalsTitle(null), null);
	assert.equal(
		vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'no_vocals', fps: 150, regions: [] })),
		'no vocals detected'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'not_analyzed' })),
		'vocals not analyzed in rekordbox'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(
			anlzWithVocals({ status: 'demucs', fps: 150, regions: [{ start_s: 1, end_s: 2, intensity: 1 }] })
		),
		'vocals: local detection'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(
			anlzWithVocals({ status: 'rekordbox', fps: 150, regions: [{ start_s: 1, end_s: 2, intensity: 1 }] })
		),
		null
	);
	assert.throws(() => vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'bogus' })), /unknown status/);
});

function classHost() {
	const classes = new Set();
	return {
		classes,
		classList: {
			toggle(token, force) {
				if (force) classes.add(token);
				else classes.delete(token);
				return classes.has(token);
			},
			remove(token) {
				classes.delete(token);
			}
		}
	};
}

test('desktop passthrough follows tech mode on html and body, and its cleanup always clears it', () => {
	const html = classHost();
	const body = classHost();
	const cleanupOn = passthrough.applyDesktopPassthrough(true, [html, body]);
	assert.deepEqual([...html.classes], ['tw-desktop-passthrough']);
	assert.deepEqual([...body.classes], ['tw-desktop-passthrough']);
	cleanupOn();
	assert.equal(html.classes.size, 0);
	assert.equal(body.classes.size, 0);

	const cleanupOff = passthrough.applyDesktopPassthrough(false, [html, body]);
	assert.equal(html.classes.size, 0, 'inactive tech mode never adds the class');
	html.classes.add('tw-desktop-passthrough');
	cleanupOff();
	assert.equal(html.classes.size, 0, 'route teardown clears it even when set from elsewhere');
});
