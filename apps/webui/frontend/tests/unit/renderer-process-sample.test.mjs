import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const sample = await loadTypeScriptModule('tests/e2e/support/renderer-process-sample.ts');

test('selectChromiumFamilyPids takes every process, not the first renderer', () => {
	// Seen live Fri 25 Sep 2026: three renderers and no gpu-process; the first
	// renderer (~72 MB) was not the page's own (~150-250 MB).
	const pids = sample.selectChromiumFamilyPids([
		{ id: 111, type: 'browser' },
		{ id: 222, type: 'renderer' },
		{ id: 333, type: 'renderer' },
		{ id: 444, type: 'utility' },
		{ id: 555, type: 'gpu-process' }
	]);
	assert.deepEqual(pids, [111, 222, 333, 444, 555]);
});

test('selectChromiumFamilyPids throws when no renderer is listed', () => {
	assert.throws(() => sample.selectChromiumFamilyPids([{ id: 1, type: 'browser' }]), /renderer/);
});

test('selectChromiumFamilyPids throws on a non-integer pid rather than dropping it', () => {
	assert.throws(
		() => sample.selectChromiumFamilyPids([{ id: 'x', type: 'renderer' }]),
		/pid/
	);
});

test('sumEngineFamilyFootprintMb sums live members (rss_mb)', () => {
	const total = sample.sumEngineFamilyFootprintMb({
		available: true,
		members: [
			{ name: 'engine', rss_mb: 120.5, source: 'live' },
			{ name: 'opendj-stems-worker', rss_mb: 40, source: 'live' }
		]
	});
	assert.equal(total, 160.5);
});

test('sumEngineFamilyFootprintMb excludes probe_log members (a stale record is not the live family)', () => {
	// Shape seen live Fri 25 Sep 2026: a 71 h old probe record merged one
	// desktop-shell member into an engine-only session.
	const total = sample.sumEngineFamilyFootprintMb({
		available: true,
		stale: true,
		members: [
			{ name: 'opendj-engine', rss_mb: 53.8, source: 'live' },
			{ name: 'unnamed', physical_footprint_mb: 1382.9, role: 'webkit-webcontent', source: 'probe_log' }
		]
	});
	assert.equal(total, 53.8);
});

test('sumEngineFamilyFootprintMb throws rather than reading unavailable telemetry as zero', () => {
	assert.throws(() => sample.sumEngineFamilyFootprintMb({ available: false }), /available/);
	assert.throws(() => sample.sumEngineFamilyFootprintMb({}), /available/);
});

test('sumEngineFamilyFootprintMb throws when no live member exists, rather than reporting zero', () => {
	assert.throws(
		() =>
			sample.sumEngineFamilyFootprintMb({
				available: true,
				members: [{ name: 'unnamed', physical_footprint_mb: 2.7, source: 'probe_log' }]
			}),
		/no live member/
	);
});

test('sumEngineFamilyFootprintMb throws on a member without a known source (older engine)', () => {
	assert.throws(
		() => sample.sumEngineFamilyFootprintMb({ available: true, members: [{ name: 'engine', rss_mb: 100 }] }),
		/source/
	);
});

test('sumEngineFamilyFootprintMb throws on a live member with no finite rss_mb, rather than skipping it', () => {
	assert.throws(
		() =>
			sample.sumEngineFamilyFootprintMb({
				available: true,
				members: [
					{ name: 'engine', rss_mb: 100, source: 'live' },
					{ name: 'stem-worker', source: 'live' }
				]
			}),
		/finite rss_mb/
	);
	assert.throws(
		() => sample.sumEngineFamilyFootprintMb({ available: true, members: ['not-an-object'] }),
		/not an object/
	);
});

test('countLiveFamilyMembers counts live members only', () => {
	const count = sample.countLiveFamilyMembers({
		available: true,
		members: [
			{ name: 'opendj-engine', rss_mb: 50, source: 'live' },
			{ name: 'unnamed', source: 'live' },
			{ name: 'unnamed', physical_footprint_mb: 2.7, source: 'probe_log' }
		]
	});
	assert.equal(count, 2);
});

test('countLiveFamilyMembers throws on unavailable telemetry or an unknown source', () => {
	assert.throws(() => sample.countLiveFamilyMembers({ available: false }), /available/);
	assert.throws(
		() => sample.countLiveFamilyMembers({ available: true, members: [{ name: 'engine' }] }),
		/source/
	);
});
