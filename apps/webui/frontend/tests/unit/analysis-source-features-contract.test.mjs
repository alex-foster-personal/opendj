import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Class guard for the analysis-source feature vocabulary (v1 launch bug,
 * Mon 5 Oct 2026): the top-bar menu offered key and waveform, but the
 * performance IPC parser accepted only beatgrid, so every non-beatgrid
 * click toasted "analysis-source feature must be beatgrid".
 *
 * Three sets must agree: the server's selection lanes (LANES in
 * apps/analysis/lane_enums.py, enforced by PUT /api/v1/analysis/source via
 * sel.check_lane), the parser's accepted features, and the features the UI
 * offers. The OpenAPI schema types `lane` as a bare string, so api-types.ts
 * carries no enum to derive from; the server tuple is read as text instead.
 *
 * Regression lines:
 * - if the parser rejects a lane the server accepts then the toggle toasts
 * - if the UI offers a feature the parser rejects then the toggle toasts
 * - if the parser accepts an unknown feature then validation went loose
 */

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));
const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));

let ipc;
let featureState;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	featureState = await loadTypeScriptModule('src/lib/rb/analysis-source-state.svelte.ts');
});

function serverLanes() {
	const text = readFileSync(`${REPO_ROOT}apps/analysis/lane_enums.py`, 'utf8');
	const match = text.match(/^LANES: tuple\[Lane, \.\.\.\] = \(([^)]*)\)/m);
	assert.ok(match, 'LANES tuple not found in apps/analysis/lane_enums.py; re-point this guard');
	const lanes = [...match[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
	assert.ok(lanes.length >= 1, 'parsed zero server lanes; the probe is broken, not the contract');
	return lanes;
}

function parses(feature) {
	return ipc.parsePerformanceCommandForTest({ type: 'analysis_source', feature, source: 'own' });
}

test('parser accepts every feature the server accepts', () => {
	const lanes = serverLanes();
	assert.ok(lanes.includes('key') && lanes.includes('waveform'), `server lanes: ${lanes.join(', ')}`);
	for (const lane of lanes) {
		assert.deepEqual(parses(lane), { type: 'analysis_source', feature: lane, source: 'own' });
	}
});

test('parser rejects a feature the server does not accept', () => {
	for (const bogus of ['vocals', 'bpm', '', 'BEATGRID', undefined, 1]) {
		assert.throws(() => parses(bogus), /analysis-source feature must be one of/);
	}
});

test('shared feature constant equals the server lane set exactly', () => {
	assert.deepEqual([...featureState.ANALYSIS_SOURCE_FEATURES], serverLanes());
});

test('every feature the UI offers is accepted by the parser', () => {
	const menu = readFileSync(`${FRONTEND_ROOT}src/lib/components/rb/AnalysisSourceMenu.svelte`, 'utf8');
	assert.match(
		menu,
		/\{#each ANALYSIS_SOURCE_FEATURES as feature \(feature\)\}/,
		'AnalysisSourceMenu no longer iterates ANALYSIS_SOURCE_FEATURES; re-point this guard at what it offers'
	);
	const offered = [...featureState.ANALYSIS_SOURCE_FEATURES];
	assert.ok(offered.length >= 1);
	for (const feature of offered) {
		for (const source of ['rekordbox', 'own']) {
			assert.doesNotThrow(
				() => ipc.parsePerformanceCommandForTest({ type: 'analysis_source', feature, source }),
				`UI offers ${feature}/${source} but the parser rejects it`
			);
		}
	}
});
