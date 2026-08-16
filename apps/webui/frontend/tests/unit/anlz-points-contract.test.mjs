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

/** The `points` default baked into the frontend's own /anlz client. */
function frontendDefaultPoints() {
	const source = readFileSync(API_RB, 'utf8');
	const match = source.match(/export async function fetchAnlz\([^)]*?points\s*=\s*(\d+)/s);
	if (match === null) {
		throw new Error(
			`could not read the fetchAnlz points default from ${API_RB}. If it moved to a ` +
				'named constant, update this test to read the constant - do not delete the check.'
		);
	}
	return Number(match[1]);
}

/** The FastAPI Query(default, ge=..., le=...) bounds on GET /{sid}/anlz. */
function backendPointsBounds() {
	const source = readFileSync(RB_ASSETS, 'utf8');
	const route = source.match(/@router\.get\("\/\{stable_id\}\/anlz"\)[\s\S]{0,600}?\)\s*->/);
	if (route === null) {
		throw new Error(`could not locate the /anlz route declaration in ${RB_ASSETS}`);
	}
	const query = route[0].match(
		/points:\s*int\s*=\s*Query\(\s*(\d+),\s*ge=(\d+),\s*le=(\d+)/
	);
	if (query === null) {
		throw new Error(
			`could not read Query(default, ge=, le=) for points in ${RB_ASSETS}. If the bound ` +
				'moved, update this test to read it - do not delete the check.'
		);
	}
	return { default: Number(query[1]), ge: Number(query[2]), le: Number(query[3]) };
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
