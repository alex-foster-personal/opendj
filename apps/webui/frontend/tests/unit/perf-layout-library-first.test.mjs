// requirement: SKINLAYOUT-13792
// [if] the library-first spec and perf-layout.css disagree on areas for either variant [then] the table lies about the page, [else stop].
// [if] library-first does not fit 1280x800 (28 + 2 x 56 + 249 + 360 = 749) [then] decks clip at the contract window, [else stop].
// [if] the browser row is not minmax(360px, 1fr) or the deck row is not at its 249 px floor [then] the prep shape is lost, [else stop].
// [if] the edge classes select by row order instead of the grid-area components [then] reordering rows breaks them, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const css = read('../../src/lib/rb/perf-layout.css');
const perfPage = read('../../src/routes/performance/+page.svelte');

let L;
before(async () => {
	L = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
});

const block = (variant) => {
	const m = css.match(
		new RegExp(
			`\\.perf-root\\[data-layout='library-first'\\]\\[data-layout-variant='${variant}'\\]\\s*\\{([^}]*)\\}`
		)
	);
	assert.ok(m, `missing css block for ${variant}`);
	return m[1];
};

test('spec and css agree on areas for both variants', () => {
	assert.deepEqual([...L.LIBRARY_FIRST_VARIANTS], ['decks-bottom', 'decks-top']);
	assert.equal(L.LIBRARY_FIRST_VARIANT_DEFAULT, 'decks-bottom');
	assert.deepEqual(
		[...L.PERFORMANCE_LAYOUT_SPEC['library-first'].areas],
		[...L.LIBRARY_FIRST_VARIANT_AREAS['decks-bottom']]
	);
	for (const v of L.LIBRARY_FIRST_VARIANTS) {
		const areas = [
			...block(v)
				.match(/grid-template-areas:([^;]+);/)[1]
				.matchAll(/'([^']+)'/g)
		].map((m) => m[1]);
		assert.deepEqual(areas, [...L.LIBRARY_FIRST_VARIANT_AREAS[v]], v);
	}
});

test('feasibility passes at 1280x800 with 749 px of floors', () => {
	const spec = L.PERFORMANCE_LAYOUT_SPEC['library-first'];
	assert.deepEqual({ ...spec.honours }, { deckLayout: false, perfView: true, pads: true });
	assert.deepEqual({ ...spec.floors }, { wavestackRows: 2, deckRowPx: 249, libraryPx: 360 });
	assert.deepEqual(L.presetContractOverflows(spec), []);
	assert.deepEqual(L.presetFitsWindow(spec, { w: 1280, h: 800 }), { ok: true });
	assert.equal(L.presetComfortNeedPx(spec, 56), 749);
});

test('browser row is minmax(360px, 1fr) and the deck row sits at its floor', () => {
	for (const v of L.LIBRARY_FIRST_VARIANTS) {
		const rows = block(v)
			.match(/grid-template-rows:([^;]+);/)[1]
			.trim()
			.split(/\s+/);
		const areas = L.LIBRARY_FIRST_VARIANT_AREAS[v];
		const rowFor = (area) => rows.join(' ').split(/\s+(?![^(]*\))/)[areas.indexOf(area)];
		assert.equal(rowFor('browser').replace(/\s/g, ''), 'minmax(360px,1fr)', v);
		assert.equal(rowFor('deckarea'), '249px', v);
	}
	assert.match(css, /2 \* \(var\(--rb-waverow-h\) \+ var\(--rb-stemwave-stack-extra, 0px\)\)/);
});

test('edge classes stay area-based, and the preset forces two decks', () => {
	assert.match(perfPage, /\.perf-root\.tw-active\.tw-bottom-visible :global\(\.rb-browser\)/);
	assert.match(
		perfPage,
		/\.perf-root\.tw-active:not\(\.tw-top-visible\) :global\(\.rb-wavestack\)/
	);
	assert.doesNotMatch(perfPage, /tw-(top|bottom)-visible[^{]*:nth-(child|of-type)/);
	assert.doesNotMatch(css, /nth-(child|of-type)/);
	assert.match(perfPage, /perfLayout === 'library-first'\)/);
	assert.throws(() => L.parseLibraryFirstVariant('sideways'), /decks-bottom\|decks-top/);
});

const higher = (a, b) => (a[0] !== b[0] ? a[0] > b[0] : a[1] !== b[1] ? a[1] > b[1] : a[2] > b[2]);
const rules = (source) =>
	[...source.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
		selector: m[1].trim(),
		body: m[2]
	}));

/** Selector specificity [ids, classes + attributes + pseudo-classes, types]; :where() counts 0, :is()/:not() its most specific argument. */
const specificity = (selector) => {
	const sum = [0, 0, 0];
	const add = (s) => s.forEach((n, i) => (sum[i] += n));
	let rest = selector;
	for (;;) {
		const m = rest.match(/:(where|is|not)\(/);
		if (m === null) break;
		let depth = 1;
		let end = m.index + m[0].length;
		for (; depth > 0; end++) {
			if (rest[end] === '(') depth++;
			if (rest[end] === ')') depth--;
		}
		if (m[1] !== 'where') {
			const args = rest
				.slice(m.index + m[0].length, end - 1)
				.split(',')
				.map(specificity);
			add(args.reduce((a, b) => (higher(b, a) ? b : a)));
		}
		rest = rest.slice(0, m.index) + ' ' + rest.slice(end);
	}
	rest = rest.replace(/\[[^\]]*\]/g, () => (sum[1]++, ' '));
	sum[0] += (rest.match(/#[\w-]+/g) ?? []).length;
	sum[1] += (rest.match(/\.[\w-]+|:(?!:)[\w-]+/g) ?? []).length;
	sum[2] += (rest.replace(/[.#:][\w-]+/g, ' ').match(/[a-z][\w-]*/gi) ?? []).length;
	return sum;
};

test('the preset rows beat the page LESS rows on specificity, not on CSS order', () => {
	// [if] a library-first grid-template-rows rule ties the page's Svelte-scoped .perf-root.deck-layout-less rule [then] injection order picks the rows and the library can land in a 56 px row, [else stop].
	const style = perfPage.slice(perfPage.indexOf('<style>'), perfPage.indexOf('</style>'));
	const lessRule = rules(style).find(
		(r) => r.selector === '.perf-root.deck-layout-less' && /grid-template-rows/.test(r.body)
	);
	assert.ok(lessRule, 'the page has no .perf-root.deck-layout-less grid-template-rows rule');
	// Svelte 5 adds its scope class once per compound selector: one more class.
	const less = specificity(lessRule.selector);
	less[1] += 1;
	assert.deepEqual(less, [0, 3, 0]);
	const presetRules = rules(css).filter(
		(r) => /data-layout-variant/.test(r.selector) && /grid-template-rows/.test(r.body)
	);
	assert.equal(presetRules.length, L.LIBRARY_FIRST_VARIANTS.length);
	for (const { selector } of presetRules) {
		const own = specificity(selector);
		assert.ok(higher(own, less), `${selector} is ${own}, not above the page's ${less}`);
	}
	assert.deepEqual(specificity('.a:where(.b, #c) :is(.d, #e)[x] p'), [1, 2, 1]);
});
