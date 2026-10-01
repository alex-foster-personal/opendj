/**
 * pin 6b1da5a8 (remainder): the lyric LINE lane got a readable backing and an
 * overlap cap; the WORD lane, the word-by-word karaoke overlay on the same
 * waveform, got neither. Its words sat on the bare waveform and a label wider
 * than the packer estimated (the active word is bold) could run into the next.
 *
 * [if] two words share a lane [then] the first is capped at the distance to
 *   the second, so they cannot overlap
 * [if] a word is the last visible one in its lane [then] it has no cap
 * [if] words are in different lanes [then] one never caps the other
 * [if] a word is drawn [then] it sits on the same 85% backing as a line
 * [if] the backing has padding [then] the text still starts on the sung onset
 *
 * Synthetic placeholder words only; no real lyric text.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}
const WORD_LANE = read('src/lib/components/rb/wave/WordLane.svelte');
const LINE_LANE = read('src/lib/components/rb/wave/LyricsLane.svelte');

let lanes;
before(async () => {
	lanes = await loadTypeScriptModule('src/lib/components/rb/wave/word-lanes.ts');
});

function packed(idx, lane, x, widthPx = 30) {
	return { word: { idx, word: `w${idx}`, start_s: idx, end_s: idx + 0.2 }, lane, widthPx, x };
}

test('a word is capped at the distance to the next word in its own lane', () => {
	const caps = lanes.laneWordMaxWidthsPx([packed(0, 0, 10), packed(1, 0, 45), packed(2, 0, 120)]);
	assert.deepEqual(caps, [35, 75, null]);
});

test('lanes cap independently, and the last word of each lane is uncapped', () => {
	const caps = lanes.laneWordMaxWidthsPx([
		packed(0, 0, 10),
		packed(1, 1, 20),
		packed(2, 0, 60),
		packed(3, 1, 200)
	]);
	assert.deepEqual(caps, [50, 180, null, null]);
});

test('a capped word never reaches past the next word in its lane', () => {
	const frame = [packed(0, 0, -12), packed(1, 1, 3), packed(2, 0, 40), packed(3, 1, 41), packed(4, 0, 300)];
	const caps = lanes.laneWordMaxWidthsPx(frame);
	for (let i = 0; i < frame.length; i += 1) {
		const next = frame.slice(i + 1).find((w) => w.lane === frame[i].lane);
		if (next === undefined) assert.equal(caps[i], null);
		else assert.ok(frame[i].x + caps[i] <= next.x, `word ${i} overlaps the next word in lane ${frame[i].lane}`);
	}
});

test('an empty frame and a single word need no caps', () => {
	assert.deepEqual(lanes.laneWordMaxWidthsPx([]), []);
	assert.deepEqual(lanes.laneWordMaxWidthsPx([packed(0, 0, 5)]), [null]);
});

test('two words in one lane at the same x is refused, not drawn at zero width', () => {
	assert.throws(() => lanes.laneWordMaxWidthsPx([packed(0, 0, 10), packed(1, 0, 10)]), /must start later/);
});

function rule(src, selector, until) {
	const from = src.indexOf(`${selector} {`);
	assert.ok(from !== -1, `expected a ${selector} rule`);
	return src.slice(from, src.indexOf(until, from));
}

test('the word lane uses the same backing as the line lane', () => {
	const backingOf = (css) =>
		css.match(/background:\s*color-mix\(in srgb, var\(--rb-bg\) (\d+)%, transparent\)/);
	const line = backingOf(rule(LINE_LANE, '.lyric-line', '.lyric-line.active'));
	const word = backingOf(rule(WORD_LANE, '.lane-word', '.lane-word.suspect'));
	assert.ok(line, 'the line lane backing is the reference');
	assert.ok(word, 'a word needs a backing mixed from the app background');
	assert.equal(word[1], line[1], 'both lanes must use one backing opacity');
	assert.equal(word[1], '85');
});

test('the word backing clips at its cap and keeps the text on the sung onset', () => {
	const css = rule(WORD_LANE, '.lane-word', '.lane-word.suspect');
	assert.match(css, /overflow:\s*hidden/);
	assert.match(css, /text-overflow:\s*ellipsis/);
	assert.match(css, /box-sizing:\s*border-box/);
	const pad = css.match(/padding:\s*0 (\d+)px/);
	const pull = css.match(/margin-left:\s*-(\d+)px/);
	assert.ok(pad && pull, 'backing padding must be cancelled by an equal negative margin');
	assert.equal(pull[1], pad[1], 'if the padding is not cancelled then every word starts late - broken');
});

test('the word lane hands each word its cap', () => {
	assert.match(WORD_LANE, /maxWidths: laneWordMaxWidthsPx\(packed\)/);
	assert.match(
		WORD_LANE,
		/style:max-width=\{maxWidth === null \? undefined : `\$\{maxWidth\}px`\}/
	);
});
