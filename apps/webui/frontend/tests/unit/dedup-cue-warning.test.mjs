/**
 * - [if] two duplicate tracks are compared and one has cue points the other lacks
 *   [then] the comparison UI shows this before a merge decision is made, [else stop].
 * - [if] a merge would discard the only copy with cue points [then] the user gets
 *   an explicit warning before confirming, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const dedupPagePath = path.join(__dirname, '../../src/routes/dedup/+page.svelte');

function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

function member(overrides) {
	return {
		stable_id: 'track-a',
		path: '/music/a.flac',
		is_canonical: true,
		similarity: null,
		title: 'A',
		artist: 'Artist',
		bpm: 120,
		key: '8A',
		duration_ms: 180000,
		rating: null,
		file_exists: true,
		cue_count: 0,
		hot_cue_count: 0,
		loop_count: 0,
		has_beatgrid: false,
		cue_positions_ms: [],
		...overrides
	};
}

let mergeCueWarning;

test('mergeCueWarning warns when survivor lacks cues another copy has', async () => {
	mergeCueWarning = (await loadTypeScriptModule('src/routes/dedup/cue-warning.ts')).mergeCueWarning;
	const message = mergeCueWarning(
		[member({ stable_id: 'survivor', cue_count: 0 }), member({ stable_id: 'alias', cue_count: 3 })],
		'survivor'
	);
	assert.equal(
		message,
		'Selected survivor has no cue points. 1 other copy has 3 cues. Merge does not copy cues onto the survivor. Continue anyway?'
	);
});

test('mergeCueWarning sums cues across multiple other copies', async () => {
	const message = mergeCueWarning(
		[
			member({ stable_id: 'survivor', cue_count: 0 }),
			member({ stable_id: 'alias-a', cue_count: 2 }),
			member({ stable_id: 'alias-b', cue_count: 3 })
		],
		'survivor'
	);
	assert.equal(
		message,
		'Selected survivor has no cue points. 2 other copies have 5 cues. Merge does not copy cues onto the survivor. Continue anyway?'
	);
});

test('mergeCueWarning is null when survivor already has cues', () => {
	assert.equal(
		mergeCueWarning(
			[member({ stable_id: 'survivor', cue_count: 1 }), member({ stable_id: 'alias', cue_count: 3 })],
			'survivor'
		),
		null
	);
});

test('mergeCueWarning is null when no member has cues', () => {
	assert.equal(
		mergeCueWarning(
			[member({ stable_id: 'survivor', cue_count: 0 }), member({ stable_id: 'alias', cue_count: 0 })],
			'survivor'
		),
		null
	);
});

test('mergeCueWarning is null for unknown survivor id', () => {
	assert.equal(mergeCueWarning([member({ stable_id: 'alias', cue_count: 3 })], 'missing'), null);
});

// REQ: LIBM-76
test('+page.svelte wires mergeCueWarning and confirm before apply', () => {
	const src = stripComments(readFileSync(dedupPagePath, 'utf8'));
	assert.match(src, /import\s*\{[^}]*mergeCueWarning[^}]*\}\s*from\s*['"]\.\/cue-warning['"]/);
	assert.match(src, /mergeCueWarning\(/);
	assert.match(src, /window\.confirm\(/);
	assert.match(src, /applyDedupMerge/);
	assert.doesNotMatch(src, /decide\([^)]*mergeCueWarning/);
	assert.match(src, /data-testid="dedup-member-cues"/);
	assert.match(src, /data-testid="dedup-member-beatgrid"/);
	assert.match(src, /no cues/);
	assert.match(src, /does not delete files or copy cue points/);
});
