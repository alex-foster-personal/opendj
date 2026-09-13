/**
 * DECKUX-07 preview strip: loop-span hot cues must not get a green .cue-letter
 * point chip; the existing orange canvas in..out band is the span marker.
 *
 * Regression lines:
 * - if every hot_cue row gets a .cue-letter then loop cues read as point markers
 * - if loop-span cues are skipped only in a comment then the defect returns
 * - if loopCues is removed from drawStripWaveform then the span disappears
 * - if .loop-chip in/out are deleted then only time chips remain (do-not-ship)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

const STRIP_WAVEFORM = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/StripWaveform.svelte', import.meta.url)
);

function readStrip() {
	return readFileSync(STRIP_WAVEFORM, 'utf8');
}

/** Extract the {#each deck.hot_cues ...}{/each} block that contains .cue-letter. */
function cueLetterEachBlock(source) {
	const match = /\{#each deck\.hot_cues[\s\S]*?\{\/each\}/.exec(source);
	assert.ok(match, 'StripWaveform must have {#each deck.hot_cues ...}{/each}');
	const block = match[0];
	assert.match(block, /class="cue-letter"/, 'cue-letter each must render .cue-letter');
	return block;
}

describe('preview strip loop hot cue letter filter (DECKUX-07)', () => {
	it('does not render an unfiltered cue-letter for every hot_cue row', () => {
		const block = cueLetterEachBlock(readStrip());
		const unfiltered =
			/\{#each deck\.hot_cues as hc \(hc\.slot\)\}\s*<span class="cue-letter"[^>]*>\{hc\.slot\}<\/span>\s*\{\/each\}/;
		assert.doesNotMatch(
			block,
			unfiltered,
			'unfiltered cue-letter each must be gated so loop-span cues are skipped'
		);
	});

	it('skips loop-span cues in control flow (is_loop and out_ms)', () => {
		const block = cueLetterEachBlock(readStrip());
		const hasGuard =
			/\{#if[^}]*is_loop[^}]*out_ms/.test(block) ||
			/\.filter\([^)]*is_loop[^)]*out_ms/.test(block) ||
			/\$derived[^;]*is_loop[^;]*out_ms/.test(readStrip());
		assert.ok(
			hasGuard,
			'loop-span skip must use is_loop and out_ms in {#if}, .filter(, or $derived'
		);
	});

	it('still renders green .cue-letter chips for non-loop hot cues', () => {
		const block = cueLetterEachBlock(readStrip());
		assert.match(block, /<span class="cue-letter"/);
		assert.match(block, /\{hc\.slot\}/);
	});

	it('still passes loopCues into drawStripWaveform for the orange canvas span', () => {
		const source = readStrip();
		assert.match(source, /loopCues/);
		assert.match(source, /drawStripWaveform\([^)]*loopCues/s);
	});

	it('keeps supplementary .loop-chip.in and .loop-chip.out time labels', () => {
		const source = readStrip();
		assert.match(source, /class="loop-chip in"/);
		assert.match(source, /class="loop-chip out"/);
	});
});
