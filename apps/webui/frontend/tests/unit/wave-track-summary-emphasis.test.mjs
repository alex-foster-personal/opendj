import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const summaryPath = path.join(
	__dirname,
	'../../src/lib/components/rb/wave/WaveTrackSummary.svelte'
);

/** Strip comments so doc prose can never satisfy a rule the CSS does not. */
function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
}

// pin e585d3b67f4d: "the title below it is too loud (full white font) and too
// wide. Empty and no track loaded for state there is too loud".
//
// - if the track name goes back to --rb-text then it carries the same weight as
//   primary UI text and reads as loud as the pin complains about -> broken.
// - if it goes BELOW --rb-text-dim then it drops under the 4.5:1 AA floor that
//   token was deliberately lightened to clear (pin 5503680a4e0f) -> broken.
// - if it is width: 100% again then a two-word title still spans the whole
//   132px gutter -> broken.
// - if the standalone EMPTY / NO ART slates keep the raised fill then the
//   no-track state is as prominent as a loaded one -> broken.

test('the waveform track name is secondary text, not primary', () => {
	const css = stripComments(readFileSync(summaryPath, 'utf8'));
	const rule = css.slice(css.indexOf('.wave-track-name {'));
	const body = rule.slice(0, rule.indexOf('}'));
	assert.match(body, /color:\s*var\(--rb-text-dim\)/, 'must use the dim token');
	assert.ok(!/color:\s*var\(--rb-text\)/.test(body), 'must not use the primary text token');
});

test('the waveform track name does not go dimmer than the AA-cleared token', () => {
	const css = stripComments(readFileSync(summaryPath, 'utf8'));
	assert.ok(
		!/\.wave-track-name[^}]*opacity:/.test(css),
		'dimming the name with opacity would put it back under the 4.5:1 AA floor'
	);
});

test('the waveform track name shrinks to its own content instead of filling the gutter', () => {
	const css = stripComments(readFileSync(summaryPath, 'utf8'));
	const rule = css.slice(css.indexOf('.wave-track-name {'));
	const body = rule.slice(0, rule.indexOf('}'));
	assert.match(body, /width:\s*fit-content/, 'a short title must render short');
	assert.match(body, /max-width:\s*100%/, 'a long title must still clip inside the gutter');
	assert.match(body, /overflow:\s*hidden/, 'clipping is what makes the hover scrub meaningful');
});

test('the no-track and no-artwork slates give up the raised fill', () => {
	const src = readFileSync(summaryPath, 'utf8');
	assert.match(
		src,
		/class="wave-art-slate visible standalone" title="No track loaded"/,
		'the EMPTY slate must be marked standalone'
	);
	assert.match(
		src,
		/class="wave-art-slate visible standalone" title="Artwork unavailable/,
		'the NO ART slate must be marked standalone'
	);
	const css = stripComments(src);
	assert.match(
		css,
		/\.wave-art-slate\.standalone \{[^}]*background:\s*transparent/,
		'a standalone slate must not paint the raised chrome fill'
	);
	assert.match(
		css,
		/\.wave-track-name\.empty \{/,
		'the empty-state name needs its own quieter treatment'
	);
});
