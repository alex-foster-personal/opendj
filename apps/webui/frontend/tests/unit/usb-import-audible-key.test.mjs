// requirement: USBIMP-01
// [if] a deck is playing when the DJ opens a stick [then] the plan lists tempo- and key-compatible tracks first [⛔️ if the whole stick mounts at once, or anything is written to the stick].
//
// Codex P2 (BLOCKING) on #6223: "compatible" must mean compatible with the key the
// deck sounds in now, not its unshifted metadata key.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let open;

before(async () => {
	open = await loadTypeScriptModule('src/lib/rb/usb-import-open.ts');
});

const deck = (extra) => ({ anlz: null, position_ms: 0, key_shift_semitones: 0, key: '8A', ...extra });

test('a KEY SYNC or manual shift moves the ranking key', () => {
	assert.equal(open.audibleDeckKey(deck({ key_shift_semitones: 0 })), '8A');
	// Control, other direction: a shift must change it, else the metadata key leaked through.
	assert.notEqual(open.audibleDeckKey(deck({ key_shift_semitones: 1 })), '8A');
	assert.equal(open.audibleDeckKey(deck({ key_shift_semitones: 1 })), '3A');
});

test('the analyzed key segment at the playhead wins over the metadata key', () => {
	const anlz = {
		key_segments: {
			status: 'ok',
			segments: [
				{ start_s: 0, key_camelot: '8A' },
				{ start_s: 60, key_camelot: '11B' }
			]
		}
	};
	assert.equal(open.audibleDeckKey(deck({ anlz, position_ms: 10_000 })), '8A');
	assert.equal(open.audibleDeckKey(deck({ anlz, position_ms: 90_000 })), '11B');
});

test('an unknown key is null, not the "--" display placeholder', () => {
	const anlz = { key_segments: { status: 'failed', reason: 'no audio' } };
	assert.equal(open.audibleDeckKey(deck({ anlz })), null);
	assert.equal(open.audibleDeckKey(deck({ key: null })), null);
});

test('the ranking context reads the audible key, not st.key', () => {
	const src = readFileSync(new URL('../../src/lib/rb/usb-import-open.ts', import.meta.url), 'utf8');
	assert.match(src, /key: audibleDeckKey\(st\)/);
	assert.doesNotMatch(src, /key: st\.key/);
});
