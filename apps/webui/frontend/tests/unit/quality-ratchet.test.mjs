import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// GET /api/v1/admin/quality-ratchet serves ops/quality/baseline.json verbatim
// (see apps/webui/server/routes/quality.py). This file covers the same fail-
// fast contract kpi-api.ts already carries for the farm ledger:
//
//   - a non-object response, a non-object metrics map, or a non-numeric
//     metric value is a hard parse error, never a junk row
//   - an HTTP failure surfaces status + body, never an empty section
//
// Regression lines:
// - if a non-numeric metric value parses without error then a string reads
//   as a number and the panel renders "undefined" or NaN silently
// - if the fetch stops validating the response shape then a malformed
//   baseline.json (missing 'metrics') renders an empty ratchet section
//   instead of a loud error
// - if the ratchet fetch stops surfacing HTTP status + body then a 500 from
//   the daemon renders as a silent empty section

let qualityApi;

test.before(async () => {
	qualityApi = await loadTypeScriptModule('src/routes/admin/quality-api.ts');
});

test('a well-formed response parses to generated + metrics intact', () => {
	const ratchet = qualityApi._parseQualityRatchetForTests({
		generated: '2026-08-18T22:28:54+00:00',
		metrics: { 'ruff.total': 2227, 'arch.contracts_broken': 0, 'duplication.percent': 0.32 }
	});
	assert.equal(ratchet.generated, '2026-08-18T22:28:54+00:00');
	assert.deepEqual(ratchet.metrics, {
		'ruff.total': 2227,
		'arch.contracts_broken': 0,
		'duplication.percent': 0.32
	});
});

test('a non-object response is refused', () => {
	assert.throws(
		() => qualityApi._parseQualityRatchetForTests('not an object'),
		/response is not an object/
	);
});

test('a non-object metrics map is refused', () => {
	assert.throws(
		() =>
			qualityApi._parseQualityRatchetForTests({
				generated: 't',
				metrics: [1, 2, 3]
			}),
		/metrics is not an object/
	);
});

test('a non-numeric metric value is refused rather than rendering as a junk row', () => {
	assert.throws(
		() =>
			qualityApi._parseQualityRatchetForTests({
				generated: 't',
				metrics: { 'ruff.total': '2227' }
			}),
		/metrics\.ruff\.total is not a number/
	);
});

test('a missing generated field is refused', () => {
	assert.throws(
		() => qualityApi._parseQualityRatchetForTests({ metrics: {} }),
		/generated is not a string/
	);
});

test('an HTTP failure surfaces the status and body, never an empty section', async () => {
	// openapi-fetch builds a Request before calling fetch; empty VITE_API_BASE
	// is fine in the browser but Node needs an absolute base to construct it.
	const qualityApiNet = await loadTypeScriptModule('src/routes/admin/quality-api.ts', {
		viteApiBase: 'https://quality-api.example.test'
	});
	const original = globalThis.fetch;
	globalThis.fetch = async () => new Response('baseline unreadable', { status: 500 });
	try {
		await assert.rejects(
			qualityApiNet.fetchQualityRatchet(),
			/GET \/api\/v1\/admin\/quality-ratchet failed \(HTTP 500\): baseline unreadable/
		);
	} finally {
		globalThis.fetch = original;
	}
});
