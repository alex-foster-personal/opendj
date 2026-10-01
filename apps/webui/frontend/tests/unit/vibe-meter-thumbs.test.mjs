/**
 * Pin 05a8586f4b43: vibe thumbs use green up / softened red down, not orange/yellow.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/VibeMeter.svelte', import.meta.url)),
	'utf8'
);

test('vibe thumb up uses green and thumb down uses muted red', () => {
	const upBlock = SRC.match(/\.vibe-thumb\.up\s*\{[\s\S]*?\}/)?.[0] ?? '';
	const downBlock = SRC.match(/\.vibe-thumb\.down\s*\{[\s\S]*?\}/)?.[0] ?? '';
	const upHover = SRC.match(/\.vibe-thumb\.up:hover\s*\{[\s\S]*?\}/)?.[0] ?? '';
	const downHover = SRC.match(/\.vibe-thumb\.down:hover\s*\{[\s\S]*?\}/)?.[0] ?? '';
	assert.match(upBlock, /vibe-thumb-up|#00c853/);
	assert.match(downBlock, /vibe-thumb-down|color-mix/);
	for (const block of [upBlock, downBlock, upHover, downHover]) {
		assert.doesNotMatch(block, /--rb-orange/);
		assert.doesNotMatch(block, /--rb-yellow/);
	}
	assert.doesNotMatch(upBlock, /--rb-accent/);
});
