import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// H14 - the frontend must never request more waveform detail than the backend
// accepts. The detail-point cap is ONE number kept by hand in two languages, so
// raising either side alone 422s every waveform load (the corpus carries the
// backend log: 27x 422 at points=9600, 8x 422 at points=38400).
//
// Regression lines:
// - if the FE fetchAnlz default exceeds the BE Query(le=...) bound then broken
// - if the FE default drops below the BE Query(ge=...) bound then broken
// - if either constant stops being a readable literal then this test fails loud
//   rather than silently passing on an unparsed file

const API_RB = fileURLToPath(new URL('../../src/lib/rb/api-rb.ts', import.meta.url));
const RB_ASSETS = fileURLToPath(new URL('../../../server/routes/rb_assets.py', import.meta.url));
const RUNTIME_POLICY = fileURLToPath(
	new URL('../../../../shared/runtime_policy.py', import.meta.url)
);

/** The `points` default baked into the frontend's own /anlz client. */
function frontendDefaultPoints() {
	const source = readFileSync(API_RB, 'utf8');
	if (!source.includes('points = defaultAnlzPoints()')) {
		throw new Error(
			`fetchAnlz must default to defaultAnlzPoints() in ${API_RB}. ` +
				'If the default moved, update this test - do not delete the check.'
		);
	}
	const pointsModule = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/runtime-policy-points.ts', import.meta.url)),
		'utf8'
	);
	const match = pointsModule.match(/let _anlzPointsDefault = (\d+)/);
	if (match === null) {
		throw new Error('could not read shipped anlz_points_default from runtime-policy-points.ts');
	}
	return Number(match[1]);
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

test('frontend anlz points default sits inside the backend Query bounds', () => {
	const fe = frontendDefaultPoints();
	const be = backendPointsBounds();

	assert.ok(Number.isInteger(fe) && fe > 0, `frontend points default must be a positive integer, got ${fe}`);
	assert.ok(
		fe <= be.le,
		`frontend fetchAnlz default points=${fe} exceeds the backend le=${be.le} bound - ` +
			'every waveform load will 422. Raise both sides together or lower the frontend.'
	);
	assert.ok(
		fe >= be.ge,
		`frontend fetchAnlz default points=${fe} is below the backend ge=${be.ge} bound - ` +
			'every waveform load will 422.'
	);
});

test('the backend anlz points default is itself a legal request', () => {
	const be = backendPointsBounds();
	assert.ok(
		be.ge <= be.default && be.default <= be.le,
		`backend points Query default=${be.default} is outside its own ge=${be.ge}..le=${be.le}`
	);
});

test('the anlz points cap is one number, not two that drift', () => {
	// Both sides declaring the same cap is the whole point of H14: a refactor
	// that raises one alone is exactly the failure the corpus recorded live.
	const fe = frontendDefaultPoints();
	const be = backendPointsBounds();
	assert.equal(
		fe,
		be.default,
		`frontend default points=${fe} and backend default points=${be.default} have drifted. ` +
			'They are one contract value - move them together.'
	);
});
