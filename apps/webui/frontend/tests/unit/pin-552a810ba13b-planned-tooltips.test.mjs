/**
 * Pin 552a810ba13b (issue #4089, anchor /performance `.rb-lit-button`): an
 * inert control's tooltip says what the control IS and will do, not just that
 * it is missing. the maintainer: "not implemented tooltips should say what it is still
 * ... They're part of roadmapping!" The planned-explainers catalog
 * (`src/lib/rb/planned-explainers.ts`) is that UI contract.
 *
 * The set of files is DERIVED, not listed: every .svelte component reachable by
 * static or dynamic import from the /performance route is scanned, so a new
 * component mounted there with the bare stub fails here without anyone
 * remembering to add it.
 *
 * Regression lines:
 * - if a /performance-mounted component shows the bare
 *   'not implemented - see PARITY-TODO' string (inline or via an INERT_TITLE
 *   constant) then the tooltip names an absence instead of the feature
 * - if one of the controls this pin converted drops its plannedTitle(...) call
 *   then it fell back to a bare or invented string
 * - if a plannedTitle id used on /performance has no catalog entry then the
 *   control throws at render instead of explaining itself
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const ROUTE = join(SRC, 'routes/performance/+page.svelte');
const BARE = 'not implemented - see PARITY-TODO';

/** Resolve a component import specifier to an absolute .svelte path, or null. */
function resolveSvelte(spec, fromFile) {
	if (!spec.endsWith('.svelte')) return null;
	let path;
	if (spec.startsWith('$lib/')) path = join(SRC, 'lib', spec.slice('$lib/'.length));
	else if (spec.startsWith('.')) path = resolve(dirname(fromFile), spec);
	else return null;
	return existsSync(path) ? path : null;
}

/** Every .svelte file reachable from the /performance route by import. */
function performanceComponents() {
	const seen = new Set();
	const queue = [ROUTE];
	while (queue.length > 0) {
		const file = queue.pop();
		if (seen.has(file)) continue;
		seen.add(file);
		const source = readFileSync(file, 'utf8');
		const specs = [
			...source.matchAll(/\bimport\s+(?:[\w${}\s,*]+\s+from\s+)?['"]([^'"]+)['"]/g),
			...source.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)
		].map((m) => m[1]);
		for (const spec of specs) {
			const target = resolveSvelte(spec, file);
			if (target !== null && !seen.has(target)) queue.push(target);
		}
	}
	return seen;
}

/** Controls this pin converted: file (relative to src/lib/components/rb) -> catalog id. */
const CONVERTED = {
	'mixer/Knob.svelte': 'mixer-knob',
	'mixer/CrossfadeCurveSelect.svelte': 'crossfade-curve',
	'FeedbackPinVisibilityActions.svelte': 'feedback-pin-visibility',
	'ContextMenu.svelte': 'context-menu-unavailable',
	'browser/TrackTable.svelte': 'track-table-filter',
	'deck/JogDial.svelte': 'quantize-grid-phase',
	'RecommendedSection.svelte': 'recommended-section'
};

let components;
let explainers;

before(async () => {
	components = performanceComponents();
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

test('the import walk reaches the components this pin is about (instrument control)', () => {
	// If the walk silently stopped at the route, every scan below would pass on
	// an empty set. Require that it reaches nested components on every surface.
	assert.ok(components.size > 40, `walk found only ${components.size} components`);
	for (const rel of Object.keys(CONVERTED)) {
		const abs = join(SRC, 'lib/components/rb', rel);
		assert.ok(components.has(abs), `${rel} is not reachable from /performance - the walk is broken`);
	}
});

test('the bare-string scan fires on a known offender (negative control)', () => {
	// TrackActions.svelte lives on /track/[id], not /performance, and still
	// carries the stub: the detector must see it there and the walk must not.
	const offsite = join(SRC, 'lib/components/TrackActions.svelte');
	assert.ok(readFileSync(offsite, 'utf8').includes(BARE), 'control fixture moved - pick another');
	assert.equal(components.has(offsite), false, 'TrackActions must not be on /performance');
});

test('no /performance-mounted component shows the bare PARITY-TODO stub as its tooltip', () => {
	const offenders = [];
	for (const file of components) {
		const source = readFileSync(file, 'utf8');
		const rel = file.slice(SRC.length + 1);
		if (source.includes(BARE)) offenders.push(`${rel}: contains '${BARE}'`);
		// A toast reporting a refused action is an error message, not a tooltip.
		else if (
			source
				.split('\n')
				.some((line) => /not implemented\W+see PARITY-TODO/i.test(line) && !line.includes('pushToast('))
		) {
			offenders.push(`${rel}: contains a reworded 'not implemented, see PARITY-TODO' stub`);
		}
		if (/const\s+INERT_TITLE\s*=\s*['"]not implemented/.test(source)) {
			offenders.push(`${rel}: declares the legacy INERT_TITLE literal`);
		}
		if (/title="[^"]*not implemented[^"]*"/i.test(source)) {
			offenders.push(`${rel}: a static title still says only 'not implemented'`);
		}
	}
	assert.deepEqual(offenders, [], offenders.join('\n'));
});

test('each converted control wires plannedTitle with a real catalog entry', () => {
	for (const [rel, id] of Object.entries(CONVERTED)) {
		const source = readFileSync(join(SRC, 'lib/components/rb', rel), 'utf8');
		assert.match(
			source,
			new RegExp(`plannedTitle\\(\\s*'${id}'\\s*\\)`),
			`${rel} must use plannedTitle('${id}')`
		);
		const title = explainers.plannedTitle(id);
		assert.match(title, /^\S.*? - \S/, `${id} must lead with "Name - what it does"`);
		assert.match(title, /\(Not built yet\.\)$/, `${id} must still say it is not built`);
	}
});

test('every literal plannedTitle id on /performance resolves in the catalog', () => {
	const missing = [];
	let seen = 0;
	for (const file of components) {
		const source = readFileSync(file, 'utf8');
		for (const m of source.matchAll(/plannedTitle\(\s*'([^']+)'\s*\)/g)) {
			seen++;
			if (!(m[1] in explainers.PLANNED_CONTROLS)) missing.push(`${file.slice(SRC.length + 1)}: ${m[1]}`);
		}
	}
	assert.ok(seen >= Object.keys(CONVERTED).length, `only ${seen} plannedTitle calls found`);
	assert.deepEqual(missing, [], missing.join('\n'));
});
