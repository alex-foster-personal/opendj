/**
 * Pin 3d9ae399ddb2: KEY SYNC and key-readout cross-notation copy.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { compile } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';

let formatKeySyncDeltaText;
let keySyncNotationBullet;

before(async () => {
	({ formatKeySyncDeltaText, keySyncNotationBullet } = await loadTypeScriptModule(
		'src/lib/rb/key-sync-copy.ts'
	));
});

test('3d9ae399ddb2: compatible 6A/7A zero delta names both keys without bare +0 semitones', () => {
	const text = formatKeySyncDeltaText('6A', '7A', 0);
	assert.match(text, /harmonically aligned/i);
	assert.match(text, /6A → G minor/);
	assert.match(text, /7A → D minor/);
	assert.doesNotMatch(text, /\+0 semitone/i);
	const bullet = keySyncNotationBullet('6A', '7A');
	assert.match(bullet, /6A → G minor/);
	assert.match(bullet, /7A → D minor/);
});

test('formatKeySyncDeltaText keeps already harmonically aligned for identical wheel positions', () => {
	assert.equal(formatKeySyncDeltaText('6A', '6A', 0), 'already harmonically aligned');
	assert.equal(formatKeySyncDeltaText('Gm', '6A', 0), 'already harmonically aligned');
});

test('formatKeySyncDeltaText preserves non-zero delta vocal-effect wording', () => {
	const up = formatKeySyncDeltaText('6A', '8A', 1);
	assert.match(up, /1 semitone up/);
	assert.match(up, /vocals slightly higher/);
	const down = formatKeySyncDeltaText('6A', '4A', -2);
	assert.match(down, /2 semitones down/);
	assert.match(down, /vocals much lower/);
});

test('3d9ae399ddb2: DeckHeader wires key-sync-copy and camelotKeyHoverLabel titles', () => {
	const headerPath = fileURLToPath(
		new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)
	);
	const source = readFileSync(headerPath, 'utf8');
	assert.doesNotThrow(() =>
		compile(source, { filename: headerPath, generate: 'server' })
	);
	assert.match(source, /from '\$lib\/rb\/key-sync-copy'/);
	assert.match(source, /formatKeySyncDeltaText/);
	assert.match(source, /keySyncNotationBullet/);
	assert.match(source, /const keyHover/);
	assert.match(source, /camelotKeyHoverLabel\(effective\)/);
	assert.match(source, /title=\{keyHover/);
	assert.match(source, /origKeyHover/);
	assert.match(source, /camelotKeyHoverLabel\(deck\.key\)/);
	assert.match(source, /title=\{origKeyHover/);
});
