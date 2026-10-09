import * as assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { scratchDir } from './helpers';

const SCRIPT = path.join(__dirname, '..', '..', 'scripts', 'stamp-build.mjs');
const LANE = 'OPENDJ_BUILD_LANE_LABEL';
const CHANNEL = 'OPENDJ_BUILD_CHANNEL';

interface Stamp {
	git_sha: string;
	git_sha_full: string;
	lane_label: string;
	release_channel: string;
}

/** Run the real script with exactly `env` for the two stamp variables; never this process's own. */
function stamp(env: Record<string, string>): { status: number | null; stderr: string; out: string } {
	const out = path.join(scratchDir('stamp'), 'build-identity.json');
	const base = { ...process.env };
	delete base[LANE];
	delete base[CHANNEL];
	const result = spawnSync(process.execPath, [SCRIPT, out], { encoding: 'utf8', env: { ...base, ...env } });
	return { status: result.status, stderr: result.stderr, out };
}

for (const [absent, present] of [
	[LANE, CHANNEL],
	[CHANNEL, LANE]
] as const) {
	test(`stamp-build refuses to stamp when ${absent} is not set, and names it`, () => {
		const run = stamp({ [present]: 'given' });
		assert.notEqual(run.status, 0, 'a missing stamp input must not exit 0');
		assert.notEqual(run.status, null, 'the script must exit, not be signaled');
		assert.ok(run.stderr.startsWith(`stamp-build: ${absent} must be set`), run.stderr);
		assert.equal(fs.existsSync(run.out), false, 'no identity file may be written without its inputs');
	});
}

test('stamp-build names both variables when neither is set', () => {
	const run = stamp({});
	assert.notEqual(run.status, 0);
	assert.ok(run.stderr.startsWith(`stamp-build: ${LANE} and ${CHANNEL} must be set`), run.stderr);
	assert.equal(fs.existsSync(run.out), false);
});

test('stamp-build stamps exactly the lane and channel it was given', () => {
	const run = stamp({ [LANE]: 'lane-under-test', [CHANNEL]: 'channel-under-test' });
	assert.equal(run.status, 0, run.stderr);
	const written = JSON.parse(fs.readFileSync(run.out, 'utf8')) as Stamp;
	assert.equal(written.lane_label, 'lane-under-test');
	assert.equal(written.release_channel, 'channel-under-test');
	assert.match(written.git_sha_full, /^[0-9a-f]{40}$/);
	assert.equal(written.git_sha, written.git_sha_full.slice(0, 12));
});

test('stamp-build accepts an explicitly empty lane and channel as none', () => {
	const run = stamp({ [LANE]: '', [CHANNEL]: '' });
	assert.equal(run.status, 0, run.stderr);
	const written = JSON.parse(fs.readFileSync(run.out, 'utf8')) as Stamp;
	assert.equal(written.lane_label, '');
	assert.equal(written.release_channel, '');
});
