/**
 * Track-table column headers that must stay icons rather than words.
 *
 * The column-header checks below are SOURCE-SHAPE ON PURPOSE, same reason as
 * capability-gating-markup.test.mjs: TrackTable.svelte is too large to mount
 * under node:test, so its header markup is read as source.
 *
 * The QualityBadge twin test further down is different: it mounts the REAL
 * QualityBadge.svelte and QualityBadge.tsx through
 * quality-badge-client-harness.mjs (only the DOM and loadHover are doubled),
 * flips trackKey, and resolves controlled loadHover promises to pin the
 * summary fallback and stale-response rejection (PRRT_kwDOSEvNd860kFHb).
 *
 * Pin e28577797642 (the maintainer, Wed 2 Sep 2026): "artwork col header -> icon.
 * (regression)." The artwork column is 54px of thumbnails; the word "Artwork"
 * does not fit it and the row of narrow columns beside it (funnel, err, cloud)
 * had already settled on the h-icon + inline-svg convention.
 *
 * Regression lines:
 * - if the artwork header renders the word "Artwork" again then it is wider
 *   than its own column for the second time
 * - if it loses the h-icon class then it is no longer centred like its
 *   neighbours and the convention has two members instead of three
 * - if it loses its column-tip explanation then the icon means nothing to a reader
 *   who has not seen the column before
 * - if filename or location lose hide rules or col guards then hidden columns still
 *   reserve width and misalign the table (#10030)
 * - if Location loses Alt+click reveal or filePathCellsFor then path columns drift
 *   from file-path-cells.ts (#10030)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import {
	createReactBadgeController,
	loadReactBadge,
	loadSvelteBadgeHarness
} from './quality-badge-client-harness.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);
const qualityBadge = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/QualityBadge.svelte', import.meta.url)),
	'utf8'
);
const qualityBadgeTsx = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/QualityBadge.tsx', import.meta.url)),
	'utf8'
);

let gradeMod = null;
/** @type {{ mountSvelteQualityBadge: (target: unknown, ctrl: unknown) => { flush: () => void, unmount: () => void } } | null} */
let svelteHarnessNs = null;
/** @type {Record<string, unknown> | null} */
let reactBundleNs = null;

before(async () => {
	gradeMod = await loadTypeScriptModule('src/lib/rb/quality-grade.ts');
	svelteHarnessNs = await loadSvelteBadgeHarness();
	reactBundleNs = await loadReactBadge();
});

function deferred() {
	let resolve;
	const promise = new Promise((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

/** The `<th ...>...</th>` whose class list contains `h-art`. */
function artHeader() {
	const start = table.indexOf('class="h-icon h-art"');
	assert.notEqual(start, -1, 'no <th class="h-icon h-art"> in TrackTable');
	const open = table.lastIndexOf('<th', start);
	const close = table.indexOf('</th>', start);
	return table.slice(open, close);
}

test('the artwork header is an icon, not the word', () => {
	const th = artHeader();
	assert.match(th, /<svg\b/, 'artwork header has no inline svg');
	assert.equal(/>\s*Artwork\s*</.test(th), false, 'the word "Artwork" is back in the header');
});

test('the artwork header keeps its accessible custom column explanation', () => {
	const header = artHeader();
	assert.match(header, /use:columnExplainer=\{\{ text: columnHeaderTitle\('art'\) \}\}/);
	assert.doesNotMatch(header, /\btitle=/, 'the native tooltip competes with the custom explainer');
});

test('it uses the same h-icon convention as its narrow neighbours', () => {
	// funnel / err / cloud / autoplay already sit on h-icon; artwork joins
	// them. Count rather than name them: the point is that h-icon is the
	// shared convention, not that any one column keeps its own modifier.
	const iconHeaders = table.match(/class="h-icon(?: [a-z-]+)?"/g) ?? [];
	assert.ok(iconHeaders.length >= 5, `only ${iconHeaders.length} h-icon headers`);
	assert.ok(table.includes('class="h-icon h-err"'));
	assert.ok(table.includes('class="h-icon h-art"'));
});

test('the quality column is compact QLT with a full custom explanation', async () => {
	const { COL_DEFAULTS } = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	assert.equal(COL_DEFAULTS.quality, 34);
	assert.match(
		table,
		/use:columnExplainer=\{\{\s*text: ["']QLT - audio quality as a letter: A safe on any system/
	);
	assert.match(table, /<span class="th-label"><span>QLT<\/span><\/span>/);
	// QLTSCAN-05: compact letter, and the full hover loads only for a graded row.
	assert.match(
		table,
		/<QualityBadge\s+quality=\{row\.quality\}\s+trackKey=\{row\.stable_id\}\s+compact\s+showContainer=\{false\}\s+loadHover=\{row\.quality\?\.grade\?\.letter\s+\? \(\) => audioQualityHover\(row\.stable_id, JSON\.stringify\(row\.quality\?\.grade\)\)\s+: undefined\}\s+\/>/
	);
	assert.match(qualityBadge, /compact \? full\.slice\(0, 1\)\.toUpperCase\(\) : full/);
	assert.match(qualityBadge, /class:compact/);
});

test('filename and location path cells dim broken rows (#10030)', () => {
	assert.match(table, /class:broken=\{rowRendersUnavailable\(row\)\}/);
	assert.match(table, /class="c-location c-path"/);
});

test('filename and location columns wire cells, Alt+click reveal, and hide rules (#10030)', () => {
	assert.match(table, /filePathCellsFor\(row\)/);
	assert.match(table, /e\.altKey.*revealTrack\(row\.stable_id\)/s);
	assert.match(table, /table\.hide-filename :is\(th\.h-filename, td\.c-filename\)/);
	assert.match(table, /table\.hide-location :is\(th\.h-location, td\.c-location\)/);
	for (const col of ['filename', 'location']) {
		assert.ok(table.includes(`{#if !hiddenCols.has('${col}')}<col`), `${col} col guard`);
	}
});

test('QualityBadge twins share qualityHoverStateKey shape for hover state', () => {
	assert.match(qualityBadge, /qualityHoverStateKey\s*\(\s*trackKey\s*,\s*quality\?\.grade\s*\)/);
	assert.match(qualityBadgeTsx, /qualityHoverStateKey\s*\(\s*trackKey\s*,\s*quality\?\.grade\s*\)/);
	assert.match(qualityBadge, /hoverKey\s*===\s*hoverStateKey/);
	assert.match(qualityBadgeTsx, /hoverKey\s*===\s*hoverStateKey/);
	assert.match(qualityBadgeTsx, /currentKey\.current\s*=\s*hoverStateKey/);
});

test('QualityBadge twins: track-keyed hover (issue #9320, PRRT_kwDOSEvNd860kFHb)', async () => {
	const grade = { letter: 'A', checked: true, issues: [] };
	const quality = {
		venue: 'stadium',
		label: 'Stadium',
		rank: 5,
		of: 6,
		blurb: 'survives the biggest room',
		kbps: 1411,
		container: '.flac',
		lossless: true,
		grade
	};
	const summary = gradeMod.gradeView(grade, true).title;

	assert.notEqual(
		gradeMod.qualityHoverStateKey('track-a', grade),
		gradeMod.qualityHoverStateKey('track-b', grade)
	);
	assert.equal(gradeMod.qualityHoverStateKey('track-a', grade), `track-a|${JSON.stringify(grade)}`);

	/** Svelte binds DOM `mouseenter`; React 19 listens to `mouseover` and synthesizes enter/leave (registerDirectEvent). */
	function hoverInEvent(kind) {
		return { type: kind === 'react' ? 'mouseover' : 'mouseenter' };
	}

	const { mountSvelteQualityBadge } = /** @type {any} */ (svelteHarnessNs);
	const { QualityBadge, React, createRoot, flushSync } = /** @type {any} */ (reactBundleNs);
	const reactCtl = createReactBadgeController(QualityBadge, React, createRoot, flushSync);

	/** @param {'svelte' | 'react'} kind */
	async function scenarioA(kind) {
		const deferredA = deferred();
		const deferredB = deferred();
		let hoverCalls = 0;
		const initialLoadHover = () => {
			hoverCalls += 1;
			return hoverCalls === 1 ? deferredA.promise : deferredB.promise;
		};
		const target = globalThis.document.createElement('div');
		globalThis.document.body.appendChild(target);
		try {
			if (kind === 'svelte') {
				const ctrl = {
					initialTrackKey: 'track-a',
					initialLoadHover,
					quality,
					update: null
				};
				const api = mountSvelteQualityBadge(target, ctrl);
				const badge = target.querySelector('.q-grade');
				assert.ok(badge, `${kind}: graded span`);
				assert.equal(badge.getAttribute('title'), summary, kind);

				badge.dispatchEvent(hoverInEvent(kind));
				deferredA.resolve('full-hover-a');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-a', kind);

				ctrl.update({ trackKey: 'track-b', loadHover: () => deferredB.promise });
				api.flush();
				assert.equal(badge.getAttribute('title'), summary, `${kind}: summary after track switch`);

				badge.dispatchEvent(hoverInEvent(kind));
				deferredB.resolve('full-hover-b');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-b', kind);
				api.unmount();
			} else {
				const api = reactCtl.mount(target, {
					quality,
					trackKey: 'track-a',
					loadHover: initialLoadHover
				});
				const badge = target.querySelector('.q-grade');
				assert.ok(badge, `${kind}: graded span`);
				assert.equal(badge.getAttribute('title'), summary, kind);

				badge.dispatchEvent(hoverInEvent(kind));
				deferredA.resolve('full-hover-a');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-a', kind);

				api.update({ trackKey: 'track-b', loadHover: () => deferredB.promise });
				assert.equal(badge.getAttribute('title'), summary, `${kind}: summary after track switch`);

				badge.dispatchEvent(hoverInEvent(kind));
				deferredB.resolve('full-hover-b');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-b', kind);
				api.unmount();
			}
		} finally {
			target.remove();
		}
	}

	/** @param {'svelte' | 'react'} kind */
	async function scenarioB(kind) {
		const deferredA = deferred();
		const deferredB = deferred();
		const target = globalThis.document.createElement('div');
		globalThis.document.body.appendChild(target);
		try {
			if (kind === 'svelte') {
				const ctrl = {
					initialTrackKey: 'track-a',
					initialLoadHover: () => deferredA.promise,
					quality,
					update: null
				};
				const api = mountSvelteQualityBadge(target, ctrl);
				const badge = target.querySelector('.q-grade');
				assert.equal(badge.getAttribute('title'), summary, kind);

				badge.dispatchEvent(hoverInEvent(kind));

				ctrl.update({ trackKey: 'track-b' });
				api.flush();
				assert.equal(badge.getAttribute('title'), summary, `${kind}: switched before stale`);

				deferredA.resolve('stale-hover-a');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), summary, `${kind}: stale in-flight rejected`);

				ctrl.update({ loadHover: () => deferredB.promise });
				api.flush();
				badge.dispatchEvent(hoverInEvent(kind));
				deferredB.resolve('full-hover-b');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-b', kind);
				api.unmount();
			} else {
				const api = reactCtl.mount(target, {
					quality,
					trackKey: 'track-a',
					loadHover: () => deferredA.promise
				});
				const badge = target.querySelector('.q-grade');
				assert.equal(badge.getAttribute('title'), summary, kind);

				badge.dispatchEvent(hoverInEvent(kind));

				api.update({ trackKey: 'track-b' });
				assert.equal(badge.getAttribute('title'), summary, `${kind}: switched before stale`);

				deferredA.resolve('stale-hover-a');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), summary, `${kind}: stale in-flight rejected`);

				api.update({ loadHover: () => deferredB.promise });
				badge.dispatchEvent(hoverInEvent(kind));
				deferredB.resolve('full-hover-b');
				await Promise.resolve();
				api.flush();
				assert.equal(badge.getAttribute('title'), 'full-hover-b', kind);
				api.unmount();
			}
		} finally {
			target.remove();
		}
	}

	await scenarioA('svelte');
	await scenarioA('react');
	await scenarioB('svelte');
	await scenarioB('react');
});
