import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');

/** Every rule body whose selector (one line, tab-indented in the component) is exactly `selector`. */
function ruleBodies(src, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	return [...src.matchAll(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`, 'g'))].map((m) => m[1]);
}

/** Custom property map for the block whose selector is exactly `selector`. */
function themeBlock(selector) {
	const bodies = ruleBodies(THEME, selector);
	assert.ok(bodies.length > 0, `no \`${selector} {}\` block in theme.css`);
	return Object.fromEntries(
		bodies.flatMap((b) => [...b.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]))
	);
}

const zIndexOf = (body) => Number(body.match(/z-index:\s*(-?\d+)/)?.[1]);

test('if an artist cell lets text spill past its column (overflow visible) then the artist ellipsis is broken', () => {
	// Any rule naming the artist cell must leave the td clip alone.
	for (const m of TABLE.matchAll(/\n\s*[^\n{}]*\.c-artist[^\n{]*\{([^}]*)\}/g)) {
		assert.doesNotMatch(m[1], /overflow:\s*visible/);
	}
	assert.match(TABLE, /<td class="c-artist"/);
	const td = ruleBodies(TABLE, ':where(tbody > tr) > :global(td)')[0];
	assert.match(td, /overflow:\s*hidden/);
	assert.match(td, /text-overflow:\s*ellipsis/);
	assert.match(td, /white-space:\s*nowrap/);
});

test('if the compatible-key ring does not paint above the row separator on all four sides then the bottom edge is hidden', () => {
	const ring = ruleBodies(TABLE, '.c-key.key-compat::after')[0];
	assert.ok(ring, 'expected a .c-key.key-compat::after overlay');
	assert.match(ring, /position:\s*absolute/);
	assert.match(ring, /inset:\s*0/);
	assert.match(ring, /box-shadow:\s*inset 0 0 0 1px var\(--rb-key-compat-border, var\(--key-compat-color\)\)/);
	const separator = ruleBodies(TABLE, 'tbody tr::after')[0];
	assert.ok(zIndexOf(ring) > zIndexOf(separator), `ring z ${zIndexOf(ring)} must exceed separator z ${zIndexOf(separator)}`);
	// The inline style hands the overlay its color rather than drawing a (clippable) td shadow.
	assert.match(TABLE, /`--key-compat-color: \$\{masterKeyColor\}`/);
	assert.doesNotMatch(TABLE, /box-shadow: inset 0 0 0 1px \$\{masterKeyColor\}/);
});

test('if the play-count header label is not centered then its icon sits off the column center', () => {
	const body = ruleBodies(TABLE, '.h-plays .th-label')[0];
	assert.ok(body, 'expected a .h-plays .th-label rule');
	assert.match(body, /justify-content:\s*center/);
});

test('if mono-dev leaves status dots, rail icons, knob arcs, key ring or vocal bars colored then the skin is not monochrome', () => {
	const base = themeBlock('.perf-root');
	const mono = themeBlock("html[data-skin='mono-dev'] .perf-root");
	// Default skin keeps color (control: these hooks must not gray every skin).
	assert.equal(base['--rb-status-dot-filter'], 'none');
	assert.equal(base['--rb-rail-spotify'], '#35c04f');
	assert.equal(base['--rb-knob-warn'], 'var(--rb-orange)');
	assert.equal(base['--rb-key-compat-border'], 'initial');
	assert.equal(base['--rb-wave-vocal'], '#4fb2ff');
	// mono-dev: grays.
	assert.equal(mono['--rb-status-dot-filter'], 'grayscale(1)');
	for (const rail of ['spotify', 'files', 'beatport', 'record']) assert.equal(mono[`--rb-rail-${rail}`], 'currentColor');
	const isGray = (hex) => /^#([0-9a-f]{2})\1\1$/i.test(hex);
	for (const name of ['--rb-knob-warn', '--rb-knob-alarm', '--rb-key-compat-border']) {
		assert.ok(isGray(mono[name]), `${name}=${mono[name]} must be a neutral gray`);
	}
	assert.equal(mono['--rb-wave-vocal'], '#cfe6ff');
});

test('if the components read hard-coded colors instead of the skin tokens then mono-dev cannot de-color them', () => {
	const dots = readFileSync('src/lib/components/rb/browser/AnalysisDots.svelte', 'utf8');
	assert.match(dots, /filter:\s*var\(--rb-status-dot-filter\)/);
	const rail = readFileSync('src/lib/components/rb/browser/IconRail.svelte', 'utf8');
	for (const name of ['spotify', 'files', 'beatport', 'record']) assert.match(rail, new RegExp(`var\\(--rb-rail-${name}\\)`));
	// Icon fills only; the live `.recording` red stays (a state, like errors).
	assert.doesNotMatch(rail.split('<style')[0], /#35c04f|#3d7dd9|#8e5bd6|#d0342c/i);
	const knobStyle = readFileSync('src/lib/components/rb/mixer/Knob.svelte', 'utf8').split('<style')[1];
	assert.match(knobStyle, /var\(--rb-knob-warn\)/);
	assert.match(knobStyle, /var\(--rb-knob-alarm\)/);
	assert.doesNotMatch(knobStyle, /var\(--rb-(orange|red)\)/);
	const strip = readFileSync('src/lib/components/rb/deck/strip-waveform-render.ts', 'utf8');
	assert.doesNotMatch(strip, /VOCAL_BLUE/);
});
