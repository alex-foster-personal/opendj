/**
 * PERFMODE-05 remaining gap: processFamilyFrom must read live members[]
 * rather than JSONL by_role_mb, so unnamed procs stay listed.
 *
 * Regression: by_role_mb drops helpers with role=null (WebKit unnamed);
 * a missing kernel field must stay absent, never 0.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let snapshot;

before(async () => {
	snapshot = await loadTypeScriptModule('src/lib/rb/process-family-snapshot.ts');
});

test('processFamilyFrom lists unnamed members[] even when by_role_mb omits them', () => {
	const body = {
		available: true,
		members: [
			{ name: 'opendj-engine', rss_mb: 151.4 },
			{ name: 'unnamed', rss_mb: 99.0 }
		],
		by_role_mb: { 'python-engine': 151.4 }
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	assert.deepEqual(
		result.members.map((member) => member.label),
		['opendj-engine', 'unnamed']
	);
	assert.equal(result.members[1].mb, 99);
});

test('processFamilyFrom prefers members[] over by_role_mb when both are present', () => {
	const body = {
		available: true,
		members: [{ name: 'opendj-desktop', rss_mb: 31.9 }],
		by_role_mb: { 'desktop-shell': 999 }
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	assert.equal(result.members.length, 1);
	assert.equal(result.members[0].label, 'opendj-desktop');
	assert.equal(result.members[0].mb, 32);
});

test('processFamilyFrom still lists a member when rss_mb is missing', () => {
	const body = {
		available: true,
		members: [{ name: 'unnamed' }]
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	assert.equal(result.members.length, 1);
	assert.equal(result.members[0].label, 'unnamed');
	assert.equal(result.members[0].mb, null);
});

test('processFamilyFrom treats kernel 0 as absent, never a reading', () => {
	const body = {
		available: true,
		members: [{ name: 'opendj-engine', rss_mb: 10 }],
		kernel_memory_pressure_level: 0
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	assert.equal(result.kernelLevel, null);
});

test('processFamilyFrom omits kernel when the field is missing', () => {
	const body = {
		available: true,
		members: [{ name: 'opendj-engine', rss_mb: 10 }]
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	assert.equal(result.kernelLevel, null);
	assert.equal(Object.hasOwn(result, 'kernelLevel'), true);
});

test('processFamilyFrom maps probe role slugs to opendj labels in by_role_mb fallback', () => {
	const body = {
		available: true,
		by_role_mb: {
			'python-engine': 120.2,
			'webkit-webcontent': 44.1,
			'unknown-role': 1.0
		}
	};

	const result = snapshot.processFamilyFrom(body);

	assert.ok(result);
	const labels = result.members.map((member) => member.label);
	assert.deepEqual(labels, ['opendj-engine', 'opendj-webcontent', 'unnamed']);
	for (const label of labels) {
		assert.doesNotMatch(label, /^python-engine$/);
		assert.doesNotMatch(label, /^webkit-/);
	}
});
