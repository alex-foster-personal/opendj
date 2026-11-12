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
	assert.match(TABLE, /<td\s+class="c-artist"/);
	const td = ruleBodies(TABLE, ':where(tbody > tr) > :global(td)')[0];
	assert.match(td, /overflow:\s*hidden/);
	assert.match(td, /text-overflow:\s*ellipsis/);
	assert.match(td, /white-space:\s*nowrap/);
});

test('if the compatible-key ring does not paint above the row separator on all four sides then the bottom edge is hidden', () => {
	const ring = ruleBodies(TABLE, '.c-key.key-compat::after')[0];
	assert.ok(ring, 'expected a .c-key.key-compat::after overlay');
	assert.match(ring, /position:\s*absolute/);
	const separator = ruleBodies(TABLE, 'tbody tr::after')[0];
	assert.ok(zIndexOf(ring) > zIndexOf(separator), `ring z ${zIndexOf(ring)} must exceed separator z ${zIndexOf(separator)}`);
	// The inline style hands the overlay its color rather than drawing a (clippable) td shadow.
	assert.match(TABLE, /`--key-compat-color: \$\{masterKeyColor\}`/);
	assert.doesNotMatch(TABLE, /box-shadow: inset 0 0 0 1px \$\{masterKeyColor\}/);
});

/** Resolve the ring rule's per-side border widths and insets the way the cascade would. */
function ringBox(body) {
	const px = (v) => Number(String(v).replace(/px$/, ''));
	const widths = { top: 0, right: 0, bottom: 0, left: 0 };
	const insets = { top: NaN, right: NaN, bottom: NaN, left: NaN };
	for (const [, prop, value] of body.matchAll(/([\w-]+):\s*([^;]+);/g)) {
		const v = value.trim();
		if (prop === 'border') {
			const w = v.match(/(\d+(?:\.\d+)?)px/);
			for (const side of Object.keys(widths)) widths[side] = w ? px(w[1]) : 0;
		} else if (/^border-(top|right|bottom|left)(-width)?$/.test(prop)) {
			const side = prop.split('-')[1];
			const w = v.match(/(\d+(?:\.\d+)?)px/);
			widths[side] = w ? px(w[1]) : 0;
		} else if (prop === 'inset') {
			const parts = v.split(/\s+/).map(px);
			const [t, r = t, b = t, l = r] = parts;
			Object.assign(insets, { top: t, right: r, bottom: b, left: l });
		} else if (/^(top|right|bottom|left)$/.test(prop)) {
			insets[prop] = px(v);
		}
	}
	return { widths, insets };
}

// LIBM-176 (the maintainer, Tue 6 Oct 2026): "bottom border on key highlights missing -
// probably part of ensuring borders don't double up".
// @pytest.mark.requirement LIBM-176
test('if the key ring bottom border is thinner than its top, or the ring ends on the row separator, then the bottom edge goes missing', () => {
	const ring = ruleBodies(TABLE, '.c-key.key-compat::after')[0];
	assert.ok(ring, 'expected a .c-key.key-compat::after overlay');
	const { widths, insets } = ringBox(ring);
	assert.ok(widths.top > 0, `ring top border is ${widths.top}px; the probe resolved no border at all`);
	assert.equal(widths.bottom, widths.top, 'ring bottom border width must equal its top');
	assert.equal(widths.left, widths.top);
	assert.equal(widths.right, widths.top);
	// An inset shadow is not a border: it is what lost the bottom edge before.
	assert.doesNotMatch(ring, /box-shadow/);
	assert.match(ring, /box-sizing:\s*border-box/);
	assert.match(ring, /border:\s*1px solid var\(--rb-key-compat-border, var\(--key-compat-color\)\)/);
	// Inside its own cell (no negative inset, so the td clip cannot cut it),
	// and its bottom edge clears the 1px row separator painted at the row bottom.
	for (const side of ['top', 'right', 'left']) assert.ok(insets[side] >= 0, `${side} inset ${insets[side]}`);
	const separator = ruleBodies(TABLE, 'tbody tr::after')[0];
	const sepHeight = Number(separator.match(/height:\s*(\d+)px/)?.[1]);
	assert.equal(sepHeight, 1, 'the row separator is the 1px line the ring must clear');
	assert.match(separator, /bottom:\s*0;/);
	assert.ok(insets.bottom >= sepHeight, `ring bottom inset ${insets.bottom}px must clear the ${sepHeight}px separator`);
	// Overshoot control: the ring must not shrink away from the top and sides too.
	assert.equal(insets.top, 0);
	assert.equal(insets.left, 0);
	assert.equal(insets.right, 0);
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
	// UX-RAIL-01 removed the files and Beatport glyphs; their tokens stay for skins.
	for (const name of ['spotify', 'record']) assert.match(rail, new RegExp(`var\\(--rb-rail-${name}\\)`));
	// Icon fills only; the live `.recording` red stays (a state, like errors).
	assert.doesNotMatch(rail.split('<style')[0], /#35c04f|#3d7dd9|#8e5bd6|#d0342c/i);
	const knobStyle = readFileSync('src/lib/components/rb/mixer/Knob.svelte', 'utf8').split('<style')[1];
	assert.match(knobStyle, /var\(--rb-knob-warn\)/);
	assert.match(knobStyle, /var\(--rb-knob-alarm\)/);
	assert.doesNotMatch(knobStyle, /var\(--rb-(orange|red)\)/);
	const strip = readFileSync('src/lib/components/rb/deck/strip-waveform-render.ts', 'utf8');
	assert.doesNotMatch(strip, /VOCAL_BLUE/);
});
