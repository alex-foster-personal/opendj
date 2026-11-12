// requirement: PERF-12046
// [if] console line mixes stretchCreate ms and audioBytes bytes [then] parser splits them, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const units = await loadTypeScriptModule('src/lib/rb/deck-load-stage-units.ts');

test('parseDeckLoadPerfConsoleLine keeps audioBytes out of the ms map', () => {
	const parsed = units.parseDeckLoadPerfConsoleLine(
		'[perf] deck-load sid=abc deck=1 stretchCreate=45 facts.audioBytes=5059178 total=100'
	);
	assert.ok(parsed);
	assert.equal(parsed.stagesMs.stretchCreate, 45);
	assert.equal(parsed.facts.audioBytes, 5_059_178);
	assert.equal(parsed.stagesMs.audioBytes, undefined);
});

test('splitDeckLoadStageMap matches NON_DURATION_STAGES', () => {
	const split = units.splitDeckLoadStageMap({
		stretchCreate: 45,
		audioBytes: 5_059_178,
		stemmed: 1,
		total: 100
	});
	assert.deepEqual(split.facts, {
		audioBytes: 5_059_178,
		stemmed: 1
	});
	assert.deepEqual(split.stagesMs, { stretchCreate: 45, total: 100 });
});
