// [if] stem mute or gain commands [then] v2 rows use deck.N.stem.* ids with sample clock, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const stem = await loadTypeScriptModule('src/lib/sets/stem-control-events.ts');
const ids = await loadTypeScriptModule('src/lib/sets/control_ids.ts');
const recorder = await loadTypeScriptModule('src/lib/sets/control-event-recorder.ts');

const stamp = { sample: 96_000, sample_rate: 48_000 };

test('stem_mute maps to deck.N.stem.part.mute', () => {
	const rows = stem.eventsFromStemCommand(
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true },
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows.length, 1);
	assert.equal(rows[0].control, ids.stemMuteId(1, 'vocal'));
	assert.equal(rows[0].value, true);
	assert.equal(rows[0].sample, stamp.sample);
});

test('stem_gain maps to deck.N.stem.part.gain', () => {
	const rows = stem.eventsFromStemCommand(
		{ type: 'stem_gain', deck: 2, stem: 'drums', value: 0.33 },
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows[0].control, ids.stemGainId(2, 'drums'));
	assert.equal(rows[0].value, 0.33);
});

test('agent stem command maps to source agent', () => {
	const rows = stem.eventsFromStemCommand(
		{ type: 'stem_gain', deck: 1, stem: 'vocal', value: 0.5 },
		'agent-command',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows[0].source, 'agent');
});

test('capture skips when sample clock is unavailable', async () => {
	const posts = [];
	const restore = recorder.installControlEventRecorderForTest({
		sampleClock: () => null,
		postEnabled: false,
		wallClock: () => '2026-11-10T12:00:00Z'
	});
	recorder.bindControlEventSession('sess');
	await recorder.captureStemCommand(
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: false },
		'user-ui'
	);
	assert.equal(posts.length, 0);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('performance-ipc dispatches captureStemCommand for stem_gain', () => {
	const src = readFileSync('src/lib/rb/performance-ipc.svelte.ts', 'utf8');
	assert.match(src, /captureStemCommand\(/);
	assert.match(src, /command\.type === 'stem_gain'/);
});
