import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H14 - the frontend must never request more waveform detail than the backend
// accepts. The detail-point cap is ONE number kept by hand in two languages, so
// raising either side alone 422s every waveform load (the corpus carries the
// backend log: 27x 422 at points=9600, 8x 422 at points=38400).
//
// Since #3739 the frontend keeps NO copy of that number: until
// GET /api/v1/settings hydrates the policy, /anlz goes out without `points`
// and the server applies its own configured default; after hydration the
// client sends the value the server published. So there is nothing on the
// frontend side left to drift, and these tests pin that shape instead.
//
// Regression lines:
// - if the FE regains a literal default before hydration then broken (it can
//   exceed an operator's MDT_ANLZ_POINTS_MAX and 422 an early deck restore)
// - if an unhydrated /anlz request carries `points` then broken
// - if a hydrated request drops `points` then broken (the opposite overshoot)
// - if the BE constants stop being readable literals this test fails loud

const API_RB = fileURLToPath(new URL('../../src/lib/rb/api-rb.ts', import.meta.url));
const RB_ASSETS = fileURLToPath(new URL('../../../server/routes/rb_assets.py', import.meta.url));
const RUNTIME_POLICY = fileURLToPath(
	new URL('../../../../shared/runtime_policy.py', import.meta.url)
);

/** fetchAnlz must default through defaultAnlzPoints(), whose seed is null. */
function frontendPointsSeed() {
	const source = readFileSync(API_RB, 'utf8');
	if (!source.includes('points: number | null = defaultAnlzPoints()')) {
		throw new Error(
			`fetchAnlz must default to defaultAnlzPoints() in ${API_RB}. ` +
				'If the default moved, update this test - do not delete the check.'
		);
	}
	const pointsModule = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/runtime-policy-points.ts', import.meta.url)),
		'utf8'
	);
	const match = pointsModule.match(/let _anlzPointsDefault: number \| null = (\S+);/);
	if (match === null) {
		throw new Error('could not read the anlz points seed from runtime-policy-points.ts');
	}
	return match[1];
}

/** Backend policy constants and the FastAPI Query wiring on GET /{sid}/anlz. */
function backendPointsBounds() {
	const policySource = readFileSync(RUNTIME_POLICY, 'utf8');
	const defaultMatch = policySource.match(/^_DEFAULT_ANLZ_POINTS_DEFAULT\s*=\s*(\d+)/m);
	const minMatch = policySource.match(/^_DEFAULT_ANLZ_POINTS_MIN\s*=\s*(\d+)/m);
	const maxMatch = policySource.match(/^_DEFAULT_ANLZ_POINTS_MAX\s*=\s*(\d+)/m);
	if (defaultMatch === null || minMatch === null || maxMatch === null) {
		throw new Error(`could not read ANLZ_POINTS_* defaults from ${RUNTIME_POLICY}`);
	}
	const assets = readFileSync(RB_ASSETS, 'utf8');
	const route = assets.match(/@router\.get\("\/\{stable_id\}\/anlz"\)[\s\S]{0,600}?\)\s*->/);
	if (route === null) {
		throw new Error(`could not locate the /anlz route declaration in ${RB_ASSETS}`);
	}
	if (!route[0].includes('runtime_policy.ANLZ_POINTS_DEFAULT')) {
		throw new Error('/anlz route must reference runtime_policy.ANLZ_POINTS_* bounds');
	}
	return {
		default: Number(defaultMatch[1]),
		ge: Number(minMatch[1]),
		le: Number(maxMatch[1])
	};
}

test('the frontend holds no anlz points default before hydration', () => {
	assert.equal(
		frontendPointsSeed(),
		'null',
		'runtime-policy-points.ts must seed _anlzPointsDefault with null so the server default applies'
	);
});

test('an unhydrated /anlz request omits points; a hydrated one sends it', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/runtime-policy-points.ts');
	assert.equal(mod.defaultAnlzPoints(), null);
	assert.equal(mod.anlzQuery(mod.defaultAnlzPoints(), 7), 'gen=7');
	mod.setAnlzPointsDefault(19200);
	assert.equal(mod.defaultAnlzPoints(), 19200);
	assert.equal(mod.anlzQuery(mod.defaultAnlzPoints(), 7), 'points=19200&gen=7');
});

test('the backend anlz points default is itself a legal request', () => {
	const be = backendPointsBounds();
	assert.ok(
		be.ge <= be.default && be.default <= be.le,
		`backend points Query default=${be.default} is outside its own ge=${be.ge}..le=${be.le}`
	);
});
