// requirement: LIBUX-48, STEM-54
// [if] Gothic (mono-dev) is on [then] the library '!' square, QLT badges and off-tempo BPMs carry no saturated hue, [else stop].
// [if] stem colours are drawn in any skin [then] INST and HARM are distinct and Gothic's stem set is low-chroma, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');
const src = (rel) => readFileSync(`src/lib/components/rb/${rel}`, 'utf8');

let colors;
before(async () => {
	colors = await loadTypeScriptModule('src/lib/rb/stem-colors.ts');
});

/** Custom-property map of the block whose selector is exactly `selector`. */
function tokens(selector) {
	const start = THEME.indexOf(`\n${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block in theme.css`);
	const body = THEME.slice(start, THEME.indexOf('}', start));
	return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

/** HSL saturation (0..1) of a #rrggbb colour. */
function saturation(hex) {
	const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
	const max = Math.max(r, g, b);
	const min = Math.min(r, g, b);
	const l = (max + min) / 2;
	return max === min ? 0 : (max - min) / (1 - Math.abs(2 * l - 1));
}

/** Smallest pairwise RGB distance (0..441) across a set of hex colours. */
function minPairDistance(hexes) {
	const rgb = hexes.map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
	let min = Infinity;
	for (let i = 0; i < rgb.length; i += 1) {
		for (let j = i + 1; j < rgb.length; j += 1) min = Math.min(min, Math.hypot(...rgb[i].map((v, k) => v - rgb[j][k])));
	}
	return min;
}

const STEMS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const isHex = (v) => /^#[0-9a-f]{6}$/i.test(v ?? '');
const GOTHIC = "html[data-skin='mono-dev'] .perf-root";

test("if the Gothic '!' square, QLT filter or BPM mismatch keep a hue then the skin is not monochrome", () => {
	const mono = tokens(GOTHIC);
	for (const name of ['--rb-issue-square', '--rb-issue-square-ink', '--rb-bpm-mismatch-text']) {
		assert.ok(isHex(mono[name]), `${name}=${mono[name]} must be a hex in mono-dev`);
		assert.equal(saturation(mono[name]), 0, `${name}=${mono[name]} must be a neutral gray`);
	}
	assert.equal(mono['--rb-quality-filter'], 'grayscale(1)');
	assert.equal(mono['--rb-bpm-mismatch-ring'], 'transparent');
	// The '!' stays legible as a warning: light square, near-black glyph.
	const lum = (hex) => parseInt(hex.slice(1, 3), 16);
	assert.ok(lum(mono['--rb-issue-square']) - lum(mono['--rb-issue-square-ink']) >= 160, 'square vs glyph contrast');
	// Mismatch is dimmer than the BPM column's normal dim text.
	assert.ok(lum(mono['--rb-bpm-mismatch-text']) < lum(mono['--rb-text-dim']), 'mismatch must be dimmer than --rb-text-dim');
	assert.match(THEME, /html\[data-skin='mono-dev'\] \.perf-root \.c-bpm:is\(\.bpm-warn, \.bpm-danger, \.bpm-critical\) \{\s*color: var\(--rb-bpm-mismatch-text\);/);
});

test('if the default skin sets these hooks then it loses its amber square, colour ramp and red rings (control)', () => {
	const base = tokens('.perf-root');
	for (const name of ['--rb-issue-square', '--rb-issue-square-ink', '--rb-bpm-mismatch-ring', '--rb-bpm-mismatch-text', ...STEMS.map((s) => `--rb-stem-${s}`)]) {
		assert.equal(base[name], 'initial', `${name} must stay unset in the default skin`);
	}
	assert.equal(base['--rb-quality-filter'], 'none');
});

test('if the components hard-code these colours then the skin tokens cannot reach them', () => {
	const square = src('browser/MinorIssueSquare.svelte');
	assert.match(square, /fill: var\(--rb-issue-square, #e8a838\)/);
	assert.match(square, /stroke: var\(--rb-issue-square-ink, #1a1a1a\)/);
	assert.doesNotMatch(square.split('<style')[0], /#[0-9a-f]{6}/i, 'no literal colour in the markup');
	assert.match(src('QualityBadge.svelte'), /filter: var\(--rb-quality-filter, none\)/);
	const table = src('browser/TrackTable.svelte');
	for (const sev of ['warn', 'danger', 'critical']) {
		assert.match(table, new RegExp(`\\.c-bpm\\.bpm-${sev} \\{ box-shadow: inset 0 0 0 \\dpx var\\(--rb-bpm-mismatch-ring, #[0-9a-f]{6}\\); \\}`));
	}
	assert.match(src('deck/StemRow.svelte'), /--chip-color:\$\{stemCssColor\(stem\.id\)\}/);
	assert.match(src('mixer/ChannelStrip.svelte'), /accentColor: stemCssColor\(stem\)/);
	assert.match(src('browser/StemTags.svelte'), /--stem-instrumental: \$\{stemCssColor\('instrumental'\)\}/);
	assert.match(src('wave/StemWaveRow.svelte'), /getPropertyValue\(`--rb-stem-\$\{stem\}`\)/);
});

test('if INST and HARM share a colour then the two chips and dials cannot be told apart', () => {
	const defaults = STEMS.map((s) => colors.STEM_COLORS[s]);
	assert.equal(new Set(defaults).size, STEMS.length, `duplicate default stem colour: ${defaults.join(' ')}`);
	assert.notEqual(colors.STEM_COLORS.instrumental, colors.STEM_COLORS.other);
	assert.ok(minPairDistance(defaults) >= 60, `default stems too close (${minPairDistance(defaults).toFixed(0)})`);
	assert.equal(colors.stemCssColor('other'), `var(--rb-stem-other, ${colors.STEM_COLORS.other})`);
	assert.equal(colors.stemColorFromToken('other', '  '), colors.STEM_COLORS.other);
	assert.equal(colors.stemColorFromToken('other', ' #86aea6 '), '#86aea6');
});

test('if Gothic stems are as saturated as the default set then they still shout over the gray chrome', () => {
	const mono = tokens(GOTHIC);
	const gothic = STEMS.map((s) => mono[`--rb-stem-${s}`]);
	assert.ok(gothic.every(isHex), `every Gothic stem token must be a hex: ${gothic.join(' ')}`);
	assert.equal(new Set(gothic).size, STEMS.length, 'Gothic stems must stay distinct');
	assert.ok(minPairDistance(gothic) >= 15, `Gothic stems too close (${minPairDistance(gothic).toFixed(0)})`);
	for (const [i, s] of STEMS.entries()) {
		assert.ok(saturation(gothic[i]) <= 0.3, `${s} ${gothic[i]} saturation ${saturation(gothic[i]).toFixed(2)} > 0.3`);
		assert.ok(saturation(gothic[i]) < saturation(colors.STEM_COLORS[s]), `${s} must be less saturated than the default`);
	}
});

test('if the saturation probe passes a vivid colour then the Gothic checks above prove nothing (mutation control)', () => {
	assert.ok(saturation('#e8a838') > 0.5, 'the default amber square must read as saturated');
	assert.ok(saturation('#9ad67a') > 0.3, 'the default INST green must read as saturated');
	assert.equal(saturation('#666666'), 0);
	assert.ok(minPairDistance(['#9ad67a', '#9ad67a']) === 0, 'identical colours must measure 0 apart');
});
