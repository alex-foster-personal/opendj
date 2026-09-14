import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const TOPBAR_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)
);
const source = readFileSync(TOPBAR_PATH, 'utf8');

test('TopBar.svelte still compiles with the performance Stage control', () => {
	compile(source, { filename: TOPBAR_PATH, generate: 'client' });
});

test('TopBar wires Stage through the shared openStage store and deck read model', () => {
	assert.match(source, /import\s*\{[^}]*openStage[^}]*\}\s*from\s*'\$lib\/lyrics\/stage-store\.svelte'/);
	assert.match(source, /import\s*\{[^}]*getDeckState[^}]*\}\s*from\s*'\$lib\/rb\/audio-engine\.svelte'/);
	assert.match(source, /openStage\(stageTarget\.stableId,\s*stageTarget\.deck\)/);
	assert.match(
		source,
		/const stageTarget = \$derived\.by\([\s\S]*?getDeckState\(deck\)[\s\S]*?st\.stable_id !== null/
	);
});

test('TopBar renders one always-present Stage button disabled when no deck is loaded', () => {
	assert.equal(
		(source.match(/class="bsm-toggle topbar-slot-stage"/g) ?? []).length,
		1,
		'exactly one Stage button must be present'
	);
	assert.match(source, /disabled=\{stageTarget === null\}/);
	assert.match(source, /aria-label="Open karaoke stage"/);
});

test('compact lyric chip is not presented as the global LYR preference being off', () => {
	const chipBlock = source.match(
		/<span\s+class="lyrics-compact-chip topbar-slot-lyr-compact"[\s\S]*?<\/span>/
	)?.[0];
	assert.ok(chipBlock, 'compact lyric-status chip markup must exist');
	assert.doesNotMatch(chipBlock, /aria-pressed/, 'the compact chip must not masquerade as the LYR toggle');
	assert.match(
		source,
		/class="bsm-toggle topbar-slot-lyr"[\s\S]*aria-pressed=\{uiPrefs\.lyrics_global\}/
	);
});
