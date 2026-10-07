// requirement: DECKUX-41
// [if] the tempo offset rounds to a positive or negative whole percent [then] it reads "+N%" or "-N%", [else stop]
// [if] the tempo offset rounds to zero [then] it reads "±0%", never "-0%" or "0.0%", [else stop]
// [if] the jog dial and the deck header show the tempo offset [then] both use the one shared formatTempoPct, [else stop]
// [if] the jog dial shows a pitch range [then] it reads "±16", not "+-16", [else stop]
// [if] the jog dial shows a BPM [then] the "." and decimals draw at half the whole part's size, [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const JOG = `${FRONTEND}/src/lib/components/rb/deck/JogDial.svelte`;
const HEADER = `${FRONTEND}/src/lib/components/rb/deck/DeckHeader.svelte`;

let fmt;

before(async () => {
	fmt = await loadTypeScriptModule('src/lib/rb/tempo-format.ts');
});

/** Percent offset -> the pitch ratio the deck stores. */
const ratio = (pct) => 1 + pct / 100;

test('DECKUX-41 formatTempoPct rounds to whole percent with an explicit sign', () => {
	assert.equal(fmt.formatTempoPct(ratio(2.4)), '+2%');
	assert.equal(fmt.formatTempoPct(ratio(-2.4)), '-2%');
	assert.equal(fmt.formatTempoPct(ratio(2.6)), '+3%');
	assert.equal(fmt.formatTempoPct(ratio(-16)), '-16%');
	assert.equal(fmt.formatTempoPct(ratio(8)), '+8%');
});

test('DECKUX-41 anything that rounds to zero reads ±0%, never -0%', () => {
	assert.equal(fmt.formatTempoPct(ratio(0)), '±0%');
	assert.equal(fmt.formatTempoPct(ratio(0.4)), '±0%');
	assert.equal(fmt.formatTempoPct(ratio(-0.4)), '±0%');
	assert.equal(fmt.formatTempoPct(1), '±0%');
});

test('DECKUX-41 the range reads ±16, and WIDE for the 100% range', () => {
	assert.equal(fmt.formatPitchRange(16), '±16');
	assert.equal(fmt.formatPitchRange(8), '±8');
	assert.equal(fmt.formatPitchRange(100), 'WIDE');
});

test('DECKUX-41 the BPM splits into the whole part and the "." plus decimals', () => {
	assert.deepEqual(fmt.splitBpmText('120.00'), { whole: '120', fraction: '.00' });
	assert.deepEqual(fmt.splitBpmText('--.--'), { whole: '--', fraction: '.--' });
	assert.deepEqual(fmt.splitBpmText('128'), { whole: '128', fraction: '' });
});

test('DECKUX-41 JogDial and DeckHeader both use the shared formatter, and the decimals draw at half size', () => {
	const jog = readFileSync(JOG, 'utf8');
	const header = readFileSync(HEADER, 'utf8');
	assert.match(jog, /const pitchText: string = \$derived\(formatTempoPct\(deck\.pitch\)\);/);
	assert.match(jog, /const rangeText: string = \$derived\(formatPitchRange\(pitchRange\)\);/);
	assert.match(header, /const tempoPctText: string = \$derived\(formatTempoPct\(deck\.pitch\)\);/);
	assert.doesNotMatch(jog, /toFixed\(1\)\}%/, 'the old one-decimal, unsigned pitch text is gone');
	assert.doesNotMatch(jog, /`\+-\$\{/, 'the old "+-" range text is gone');
	assert.match(jog, /class="bpm"\s*\n?\s*>\{bpmParts\.whole\}<tspan class="bpm-fraction">\{bpmParts\.fraction\}<\/tspan>/);
	const bpm = Number(/\.dial \.bpm \{[^}]*font-size: ([\d.]+)px/.exec(jog)?.[1]);
	const frac = Number(/\.dial \.bpm-fraction \{[^}]*font-size: ([\d.]+)px/.exec(jog)?.[1]);
	assert.ok(bpm > 0 && frac > 0, `font sizes found: bpm ${bpm}, fraction ${frac}`);
	assert.equal(frac, bpm / 2, 'the decimals are half the BPM size');
});
