// requirement: IOPIN-11
// [if] Gothic (mono-dev) is on [then] a key+BPM-compatible library row gets no row wash at rest or on hover, while the key cell keeps its compatible ring, [else stop].
// [if] light (Gothic flipped, the maintainer Wed 7 Oct 2026) is on [then] it drops the wash the same way, [else stop].
// [if] the default skin is on [then] the compatible row keeps its green wash, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');
const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');

/** Custom-property map of the block whose selector is exactly `selector`. */
function tokens(selector) {
	const start = THEME.indexOf(`\n${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block in theme.css`);
	const body = THEME.slice(start, THEME.indexOf('\n}', start));
	return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

/** The `background` of the TrackTable rule whose selector starts with `prefix`. */
function rowBackground(prefix) {
	const start = TABLE.indexOf(prefix);
	assert.ok(start !== -1, `no rule starting \`${prefix}\` in TrackTable.svelte`);
	const body = TABLE.slice(TABLE.indexOf('{', start), TABLE.indexOf('}', start));
	const m = body.match(/background:\s*([^;]+);/);
	assert.ok(m, `rule \`${prefix}\` sets no background`);
	return m[1].trim();
}

/** Resolve `var(--x, fallback)` against a token map; `initial` means unset. */
function resolve(value, map) {
	const m = value.match(/^var\((--[\w-]+),\s*([\s\S]+)\)$/);
	if (!m) return value;
	const set = map[m[1]];
	return set === undefined || set === 'initial' ? m[2].trim() : set;
}

const REST = 'tbody tr.rb-row-mix-compatible:not(';
const HOVER = 'tbody tr.rb-row-mix-compatible:hover:not(';

test('Gothic and light drop the compatible-row wash at rest and on hover', () => {
	for (const skin of ['mono-dev', 'light']) {
		const map = { ...tokens('.perf-root'), ...tokens(`html[data-skin='${skin}'] .perf-root`) };
		assert.equal(resolve(rowBackground(REST), map), 'transparent', skin);
		// Hover reads like any other row (the plain tbody tr:hover raise), not green.
		assert.equal(resolve(rowBackground(HOVER), map), 'var(--rb-panel-raised)', skin);
	}
	assert.equal(rowBackground('tbody tr:hover:not(.rb-row-selected)'), 'var(--rb-panel-raised)');
});

test('control: the default skin keeps the green wash', () => {
	const map = tokens('.perf-root');
	assert.match(resolve(rowBackground(REST), map), /var\(--rb-green\) 9%/);
	assert.match(resolve(rowBackground(HOVER), map), /var\(--rb-green\) 15%/);
});

test('Gothic keeps the key-cell compatible ring as the match cue', () => {
	const gothic = tokens("html[data-skin='mono-dev'] .perf-root");
	assert.match(TABLE, /\.c-key\.key-compat::after[\s\S]*?var\(--rb-key-compat-border/);
	assert.notEqual(gothic['--rb-key-compat-border'], 'transparent');
	assert.ok(gothic['--rb-key-compat-border'], 'Gothic sets a key-compat ring color');
});
