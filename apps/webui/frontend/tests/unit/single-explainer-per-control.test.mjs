/**
 * One explainer per control (the maintainer, Tue 6 Oct 2026: "double hover explainers
 * still happening"). Hovering AutoPlay in the /performance top bar drew the
 * rich AutoPlay card AND a second "AutoPlay OFF - ..." tooltip, because the
 * button inside the card's hover host still carried `title=`.
 *
 * A hover-card host is an element whose pointer/mouse enter opens a rich card.
 * The rule, over every .svelte component reachable from /performance:
 * - no element in a host's trigger region carries `title=` (the card is the
 *   explainer; put the status line in the card). The card body itself
 *   (`data-hover-card`) may hold titled sub-controls, which are other controls.
 * - children passed into a hover-card component, and the element that wraps
 *   it, carry no `title=` either (the cross-file form of the same double).
 * - every enter handler is classified as a card host or as plain hover state,
 *   so a new card cannot appear without this test seeing it.
 * The runtime half (no box inside `data-custom-tip`) is in
 * single-hover-tooltip.test.mjs.
 *
 * Regression lines:
 * - if a hover-card host or its trigger region carries title= then two explainers show at once
 * - if a hover-card component's usage wraps or wraps into a titled element then two explainers show
 * - if a new enter handler is unclassified then a new card can double unseen
 * - if a known exception no longer exists then the exception list is stale
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { basename, dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { parse } from 'svelte/compiler';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const ROUTE = join(SRC, 'routes/performance/+page.svelte');
const ENTER_ATTRS = new Set(['onpointerenter', 'onmouseenter']);

/**
 * Enter handlers that open a rich hover card, keyed `file#hostKey`
 * (file relative to src/, hostKey = first static class, else the handler).
 */
const CARD_HOSTS = new Set([
	'lib/components/rb/TopBar.svelte#ap-wrap',
	'lib/components/rb/AnalysisSourceToggle.svelte#src-wrap',
	'lib/components/rb/RefreshAnalysisButton.svelte#wrap',
	'lib/components/rb/PerfMeters.svelte#perf-meters-root',
	'lib/components/rb/deck/ControlExplainer.svelte#explainer',
	'lib/components/rb/browser/AutoPlayExplainer.svelte#ap-explain-wrap',
	'lib/components/rb/browser/CompatibleFilterPopover.svelte#compat-filter-pop',
	'lib/components/rb/browser/AnalysisDotsPopover.svelte#wrap',
	'lib/components/rb/browser/LyricColumn.svelte#c-lyrics',
	'lib/components/rb/browser/LibrarySourceTabs.svelte#icon-btn'
]);

/** Card bodies that keep the card open while hovered: not hosts themselves. */
const CARD_BODIES = new Set([
	'lib/components/rb/TopBar.svelte#ap-menu',
	'lib/components/rb/AnalysisSourceMenu.svelte#src-menu',
	'lib/components/rb/deck/ControlExplainer.svelte#pop',
	'lib/components/rb/browser/AutoPlayExplainer.svelte#ap-explain-panel',
	'lib/components/rb/browser/CompatibleFilterPanel.svelte#compat-filter-panel',
	'lib/components/rb/browser/LyricColumn.svelte#onpointerenter={_cancelClose}',
	'lib/components/rb/browser/LyricTip.svelte#lyric-tip'
]);

/**
 * Doubles that remain, each suppressed at runtime by `data-custom-tip` on the
 * host (no visible second box), but whose `title=` is not removed here:
 * - TrackTable.svelte / BrowserPanel.svelte are single-owned by another worker
 *   on Tue 6 Oct 2026; routed through the preview-lane lead.
 * - LyricColumn cell titles are asserted by tests/e2e/lyrics-words.spec.ts.
 * - The playlist undo icon must carry title= under CHROME-01.
 * Keyed `file#line-text-fragment`. A stale entry fails the last test.
 */
const KNOWN_EXCEPTIONS = new Map([
	['lib/components/rb/browser/LyricColumn.svelte#lyr-dash', 'e2e lyrics-words asserts the cell title'],
	['lib/components/rb/browser/LyricColumn.svelte#lyr-glyph', 'e2e lyrics-words asserts the cell title'],
	['lib/components/rb/browser/LyricColumn.svelte#lyr-pct', 'e2e lyrics-words asserts the cell title'],
	[
		'lib/components/rb/browser/LibrarySourceTabs.svelte#icon-btn',
		'CHROME-01 (ui-chrome-no-emoji) requires title= on the undo icon; its text is also in the history card'
	],
	['lib/components/rb/browser/TrackTable.svelte#tr', 'TrackTable owned by another worker']
]);

// ---------------------------------------------------------------- import walk

/** Resolve a local import to a .svelte or .ts file, or null. */
function resolveLocal(spec, fromFile) {
	let base;
	if (spec.startsWith('$lib/')) base = join(SRC, 'lib', spec.slice('$lib/'.length));
	else if (spec.startsWith('.')) base = resolve(dirname(fromFile), spec);
	else return null;
	const candidates = spec.endsWith('.svelte')
		? [base]
		: [base, `${base}.ts`, join(base, 'index.ts')].filter((p) => p.endsWith('.ts'));
	return candidates.find((p) => existsSync(p)) ?? null;
}

/**
 * Every .svelte file reachable from the /performance route, following .ts
 * barrels too (BrowserPanel gets CompatibleFilterPopover via
 * browser-panel-support.ts).
 */
function performanceComponents() {
	const seen = new Set();
	const queue = [ROUTE];
	while (queue.length > 0) {
		const file = queue.pop();
		if (seen.has(file)) continue;
		seen.add(file);
		const source = readFileSync(file, 'utf8');
		const specs = [
			...source.matchAll(/\b(?:import|export)\s+(?:[\w${}\s,*]+\s+from\s+)?['"]([^'"]+)['"]/g),
			...source.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)
		].map((m) => m[1]);
		for (const spec of specs) {
			const target = resolveLocal(spec, file);
			if (target !== null && !seen.has(target)) queue.push(target);
		}
	}
	return new Set([...seen].filter((f) => f.endsWith('.svelte')));
}

// ---------------------------------------------------------------- AST helpers

const ELEMENT_TYPES = new Set(['RegularElement', 'SvelteElement']);
const COMPONENT_TYPES = new Set(['Component', 'SvelteComponent', 'SvelteSelf']);

/** Child fragments of a template node (elements, components and blocks). */
function childFragments(node) {
	return [
		node.fragment,
		node.consequent,
		node.alternate,
		node.body,
		node.fallback,
		node.pending,
		node.then,
		node.catch
	].filter((f) => f !== undefined && f !== null && Array.isArray(f.nodes));
}

/** Depth-first walk with the ancestor chain; `visit` returns false to prune. */
function walk(fragment, visit, ancestors = []) {
	for (const node of fragment.nodes) {
		if (visit(node, ancestors) === false) continue;
		for (const frag of childFragments(node)) walk(frag, visit, [...ancestors, node]);
	}
}

function attr(node, name) {
	return (node.attributes ?? []).find((a) => a.type === 'Attribute' && a.name === name);
}

function staticClass(node) {
	const a = attr(node, 'class');
	if (a === undefined || !Array.isArray(a.value)) return null;
	const text = a.value.map((v) => (v.type === 'Text' ? v.data : ' ')).join('');
	return text.trim().split(/\s+/)[0] || null;
}

function hostKey(rel, node, source) {
	const cls = staticClass(node);
	if (cls !== null) return `${rel}#${cls}`;
	const enter = (node.attributes ?? []).find((a) => ENTER_ATTRS.has(a.name));
	return `${rel}#${source.slice(enter.start, enter.end)}`;
}

function isElement(node) {
	return ELEMENT_TYPES.has(node.type);
}

/** Titled elements under `node` (inclusive), skipping card bodies. */
function titledInTriggerRegion(node) {
	const out = [];
	const check = (n) => {
		if (isElement(n) && attr(n, 'data-hover-card') !== undefined) return false;
		if (isElement(n) && attr(n, 'title') !== undefined) out.push(n);
		return true;
	};
	if (check(node) === false) return out;
	for (const frag of childFragments(node)) walk(frag, check);
	return out;
}

function describe(rel, node, source) {
	const line = source.slice(0, node.start).split('\n').length;
	const cls = staticClass(node);
	return { key: `${rel}#${cls ?? node.name}`, where: `${rel}:${line} <${node.name}${cls ? ` class="${cls}"` : ''}>` };
}

// ---------------------------------------------------------------- scan

let scan;

before(() => {
	const files = performanceComponents();
	const hosts = [];
	const unclassified = [];
	const parsed = new Map();
	for (const file of files) {
		const source = readFileSync(file, 'utf8');
		const rel = relative(SRC, file);
		const ast = parse(source, { modern: true });
		parsed.set(file, { rel, source, ast });
		walk(ast.fragment, (node) => {
			if (!isElement(node)) return true;
			if (!(node.attributes ?? []).some((a) => ENTER_ATTRS.has(a.name))) return true;
			const key = hostKey(rel, node, source);
			if (CARD_HOSTS.has(key)) hosts.push({ file, rel, source, node, key });
			else if (!CARD_BODIES.has(key)) unclassified.push(key);
			return true;
		});
	}
	// Components whose markup holds a card host: their usages are hosts too.
	// ControlExplainer is the exception by design (#5397): its child keeps
	// title= as the only explainer when the card is not rich, and the explainer
	// sets data-custom-tip itself when it is, so the parked title never shows.
	const hostComponents = new Set(
		hosts.map((h) => basename(h.file, '.svelte')).filter((name) => name !== 'ControlExplainer')
	);
	scan = { files, parsed, hosts, unclassified, hostComponents };
});

function violations() {
	const out = [];
	for (const h of scan.hosts) {
		for (const n of titledInTriggerRegion(h.node)) out.push(describe(h.rel, n, h.source));
	}
	for (const { rel, source, ast } of scan.parsed.values()) {
		walk(ast.fragment, (node, ancestors) => {
			if (!COMPONENT_TYPES.has(node.type) || !scan.hostComponents.has(node.name)) return true;
			for (const frag of childFragments(node)) {
				walk(frag, (n) => {
					if (isElement(n) && attr(n, 'data-hover-card') !== undefined) return false;
					if (isElement(n) && attr(n, 'title') !== undefined) out.push(describe(rel, n, source));
					return true;
				});
			}
			const wrapper = [...ancestors].reverse().find(isElement);
			if (wrapper !== undefined && attr(wrapper, 'title') !== undefined) {
				out.push(describe(rel, wrapper, source));
			}
			return true;
		});
	}
	return out;
}

test('the import walk reaches the top bar and the hover-card hosts (instrument control)', () => {
	assert.ok(scan.files.size > 40, `walk found only ${scan.files.size} components`);
	const found = new Set(scan.hosts.map((h) => h.key));
	for (const key of CARD_HOSTS) assert.ok(found.has(key), `card host ${key} not found - rename or walk broken`);
});

test('every pointer/mouse enter handler on /performance is classified', () => {
	// Hover-state handlers (deck focus, knob hover, scrub) are listed here so a
	// NEW card host cannot slip in unclassified.
	const HOVER_STATE = new Set([
		'lib/components/rb/AutoPlayStallBanner.svelte#ap-stall-root',
		'lib/components/rb/Deck.svelte#rb-deck',
		'lib/components/rb/RecommendedSection.svelte#rec-paired-row',
		'lib/components/rb/SuggestNextStrip.svelte#cand',
		'lib/components/rb/QuickDrawMenu.svelte#qd',
		'lib/components/rb/QuickDrawMenu.svelte#qd-item',
		'lib/components/rb/browser/RatingStars.svelte#rb-star',
		'lib/components/rb/browser/PlaylistTree.svelte#row',
		'lib/components/rb/browser/TrackTable.svelte#c-bpm',
		'lib/components/rb/browser/TrackTable.svelte#onpointerenter={() => (hoveredApId = row.stable_id)}',
		'lib/components/rb/mixer/VFader.svelte#rb-fader',
		'lib/components/rb/wave/WaveTrackSummary.svelte#wave-track-name',
		'lib/components/rb/wave/WaveRow.svelte#rb-waverow',
		'lib/components/rb/deck/HotCueBank.svelte#cue-col',
		'lib/components/rb/deck/LoopCluster.svelte#loop-cluster',
		'lib/components/rb/mixer/Knob.svelte#knob',
		'lib/components/rb/mixer/ChannelStrip.svelte#strip',
		'lib/components/rb/browser/AutoPlayRankCell.svelte#ap-rank'
	]);
	const missing = scan.unclassified.filter((k) => !HOVER_STATE.has(k));
	assert.deepEqual(missing, [], 'classify each as a CARD_HOSTS entry or as plain hover state');
});

test('every hover-card host suppresses the single-hover box (data-custom-tip on it or its trigger)', () => {
	const bare = [];
	for (const h of scan.hosts) {
		let marked = attr(h.node, 'data-custom-tip') !== undefined;
		for (const frag of childFragments(h.node)) {
			walk(frag, (n) => {
				if (isElement(n) && attr(n, 'data-hover-card') !== undefined) return false;
				if (isElement(n) && attr(n, 'data-custom-tip') !== undefined) marked = true;
				return true;
			});
		}
		if (!marked) bare.push(h.key);
	}
	assert.deepEqual(bare, [], 'a host without data-custom-tip draws its card AND the title box');
});

test('no hover-card host, trigger region or wrapped usage carries title= (one explainer per control)', () => {
	const found = violations().filter((v) => !KNOWN_EXCEPTIONS.has(v.key));
	assert.deepEqual(
		found.map((v) => v.where),
		[],
		'move the text into the card (header/status line) and drop title='
	);
});

test('the AutoPlay card carries the status lines its buttons used to put on title=', () => {
	const topbar = readFileSync(join(SRC, 'lib/components/rb/TopBar.svelte'), 'utf8');
	assert.match(topbar, /class="ap-wrap topbar-slot-autoplay"[\s\S]*?data-custom-tip=""/);
	assert.match(topbar, /class="ap-menu"\s+data-hover-card=""/);
	assert.match(topbar, /data-testid="ap-menu-status">\{autoPlayTitle\}/);
	assert.match(topbar, /data-testid="ap-menu-next">\{autoPlayNextLine\}/);
});

test('known exceptions still exist (the list is not stale)', () => {
	const keys = new Set(violations().map((v) => v.key));
	for (const [key, why] of KNOWN_EXCEPTIONS) {
		assert.ok(keys.has(key), `exception ${key} (${why}) no longer occurs - delete it`);
	}
});
