// Unit tests for scripts/bundle-charge.mjs (#10886): fixture measurements, no real build.
// Each assertion is one acceptance line of the issue.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
	applyCharges,
	chargeDate,
	NOISE_BYTES,
	planCharges
} from '../../scripts/bundle-charge.mjs';
import { BUDGETS_DIR, chargeBranchId, loadBudgets } from '../../scripts/bundle-budget-load.mjs';

const BRANCH = 'af--test-branch';
const totals = (entries) => ({
	totals: Object.fromEntries(Object.entries(entries).map(([k, bytes]) => [k, { bytes, files: 1 }]))
});
const BASELINE = totals({ library: 300000, performance: 200000, 'other-lazy': 100000 });
const CURRENT = totals({
	library: 300000 + 1500,
	performance: 200000 + NOISE_BYTES,
	'other-lazy': 100000 - 50
});

function _snapshot(dir) {
	const out = {};
	const walk = (path) => {
		for (const entry of readdirSync(path)) {
			const next = join(path, entry);
			if (statSync(next).isDirectory()) walk(next);
			else out[next] = readFileSync(next, 'utf8');
		}
	};
	walk(dir);
	return out;
}

function _run() {
	const budgetsDir = join(mkdtempSync(join(tmpdir(), 'bundle-charge-')), 'budgets');
	cpSync(BUDGETS_DIR, budgetsDir, { recursive: true });
	const before = _snapshot(budgetsDir);
	const plan = planCharges({
		baseline: BASELINE,
		current: CURRENT,
		branch: BRANCH,
		sha: 'abcdef1234',
		date: chargeDate(new Date('2026-11-10T12:00:00Z'))
	});
	const lines = applyCharges(plan, { budgetsDir, branch: BRANCH });
	return { budgetsDir, before, lines };
}

test('if a surface that grew by more than the noise allowance gets no charge file then broken', () => {
	const { budgetsDir, lines } = _run();
	const charge = JSON.parse(
		readFileSync(join(budgetsDir, 'library', 'charges', `${BRANCH}.json`), 'utf8')
	);
	assert.equal(charge.bytes, 2048);
	assert.equal(charge.measured_with_pr - charge.measured_main, 1500);
	assert.ok(lines.includes('library +1500 charged'));
});

test('if a surface that did not grow gets a charge file then broken', () => {
	const { budgetsDir, lines } = _run();
	assert.equal(existsSync(join(budgetsDir, 'performance', 'charges', `${BRANCH}.json`)), false);
	assert.equal(existsSync(join(budgetsDir, 'other-lazy', 'charges', `${BRANCH}.json`)), false);
	assert.ok(lines.includes(`performance +${NOISE_BYTES} within noise`));
	assert.ok(lines.includes('other-lazy -50 within noise'));
});

test("if a written charge file fails the gate's own schema check then broken", () => {
	const { budgetsDir } = _run();
	assert.doesNotThrow(() => loadBudgets(budgetsDir));
	assert.equal(chargeDate(new Date('2026-11-10T12:00:00Z')), 'Tue 10 Nov 2026');
});

test("if the command edits any limit, base file or other PR's charge then broken", () => {
	const { budgetsDir, before } = _run();
	const after = _snapshot(budgetsDir);
	const added = Object.keys(after).filter((path) => !(path in before));
	assert.deepEqual(added, [join(budgetsDir, 'library', 'charges', `${BRANCH}.json`)]);
	for (const [path, text] of Object.entries(before))
		assert.equal(after[path], text, `${path} changed`);
});

test('if a branch name that is a bare PR number or has a slash is accepted then broken', () => {
	for (const branch of ['1234', 'af/slash'])
		assert.throws(() => applyCharges([], { budgetsDir: tmpdir(), branch }));
});

test('if a git branch with a slash is not mapped to a charge file id then broken', () => {
	assert.equal(chargeBranchId('seer/fix-rust-deck-load-title'), 'seer--fix-rust-deck-load-title');
	assert.doesNotThrow(() =>
		applyCharges([], { budgetsDir: tmpdir(), branch: chargeBranchId('af/slash') })
	);
});
