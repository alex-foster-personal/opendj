import * as assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { readBuildStamp, shellBuildIdentity } from '../src/policy';
import { resolveProgram, scratchDir } from './helpers';

const SCRIPT = path.join(__dirname, '..', '..', 'scripts', 'stamp-build.mjs');
const VERIFY = path.join(__dirname, '..', '..', 'scripts', 'verify-pack.mjs');
const EVIDENCE = 'OPENDJ_BUILD_EVIDENCE_AT_UTC';
const LANE = 'OPENDJ_BUILD_LANE_LABEL';
const CHANNEL = 'OPENDJ_BUILD_CHANNEL';

interface Stamp {
	git_sha: string;
	git_sha_full: string;
	lane_label: string;
	release_channel: string;
}

/** Run the real script with exactly `env` for the two stamp variables; never this process's own. */
function stamp(env: Record<string, string>, extraArgs: string[] = []): { status: number | null; stderr: string; out: string } {
	const out = path.join(scratchDir('stamp'), 'build-identity.json');
	const base = { ...process.env };
	delete base[LANE];
	delete base[CHANNEL];
	delete base[EVIDENCE];
	const result = spawnSync(process.execPath, [SCRIPT, out, ...extraArgs], { encoding: 'utf8', env: { ...base, ...env } });
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

// ----- payload manifest as the identity source (EL-17-F12, EL-17-F4) -----------------
const HEAD = execFileSync(resolveProgram('git'), ['rev-parse', 'HEAD'], { cwd: __dirname, encoding: 'utf8' }).trim();

/** A payload manifest shaped like build_engine_payload.py + stable_evidence stamp-payload write it. */
function manifest(identity: Record<string, unknown>): string {
	const dir = path.join(scratchDir('manifest'), 'payload');
	fs.mkdirSync(dir, { recursive: true });
	const full = {
		git_sha: HEAD.slice(0, 8),
		git_sha_full: HEAD,
		git_branch: 'af--manifest-branch',
		git_dirty: false,
		built_at_utc: '2026-10-27T09:00:00Z',
		lane_label: 'manifest-lane',
		release_channel: 'stable',
		evidence_written_at_utc: '2026-10-27T08:30:00Z',
		...identity
	};
	fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify({ schema: 1, identity: full }));
	return path.join(dir, 'manifest.json');
}

test('stamp-build takes every identity field from the payload manifest, not the checkout or clock', () => {
	const run = stamp({}, ['--manifest', manifest({})]);
	assert.equal(run.status, 0, run.stderr);
	const written = JSON.parse(fs.readFileSync(run.out, 'utf8')) as Record<string, string>;
	assert.deepEqual(written, {
		git_sha: HEAD.slice(0, 8),
		git_sha_full: HEAD,
		git_branch: 'af--manifest-branch',
		git_dirty: '0',
		built_at_utc: '2026-10-27T09:00:00Z',
		lane_label: 'manifest-lane',
		release_channel: 'stable',
		evidence_written_at_utc: '2026-10-27T08:30:00Z'
	});
	// The page sees the evidence time through the same reader the shell uses.
	const identity = shellBuildIdentity('0.0.0', readBuildStamp(run.out));
	assert.equal(identity.evidence_written_at_utc, '2026-10-27T08:30:00Z');
	assert.equal(identity.git_branch, 'af--manifest-branch');
});

test('stamp-build refuses a payload built from another commit and names both shas', () => {
	const other = 'f'.repeat(40);
	const run = stamp({}, ['--manifest', manifest({ git_sha_full: other, git_sha: other.slice(0, 8) })]);
	assert.notEqual(run.status, 0);
	assert.notEqual(run.status, null);
	assert.ok(run.stderr.includes(other) && run.stderr.includes(HEAD), run.stderr);
	assert.equal(fs.existsSync(run.out), false);
});

test('stamp-build refuses a manifest field that is null or absent, as manifest_stamp does', () => {
	for (const broken of [{ built_at_utc: null }, { evidence_written_at_utc: undefined }]) {
		const run = stamp({}, ['--manifest', manifest(broken)]);
		assert.notEqual(run.status, 0, JSON.stringify(broken));
		assert.equal(fs.existsSync(run.out), false);
	}
});

test('stamp-build refuses a lane or channel variable that disagrees with the manifest', () => {
	const run = stamp({ [LANE]: 'some-other-lane' }, ['--manifest', manifest({})]);
	assert.notEqual(run.status, 0);
	assert.ok(run.stderr.includes(LANE) && run.stderr.includes('manifest-lane'), run.stderr);
	// Control: the same variable agreeing with the manifest is accepted.
	assert.equal(stamp({ [LANE]: 'manifest-lane', [CHANNEL]: 'stable' }, ['--manifest', manifest({})]).status, 0);
});

test('stamp-build without a payload carries OPENDJ_BUILD_EVIDENCE_AT_UTC, and omits it when unset', () => {
	const withEvidence = stamp({ [LANE]: '', [CHANNEL]: '', [EVIDENCE]: '2026-10-27T07:00:00Z' });
	assert.equal(withEvidence.status, 0, withEvidence.stderr);
	assert.equal(readBuildStamp(withEvidence.out).evidence_written_at_utc, '2026-10-27T07:00:00Z');
	const without = stamp({ [LANE]: '', [CHANNEL]: '' });
	assert.equal(readBuildStamp(without.out).evidence_written_at_utc, undefined);
});

test('stamp-build adds channel and evidence time to a manifest straight from build_engine_payload, as stamp-payload does', () => {
	// build_manifest() writes neither field; the Tauri recipe adds them via scripts.stable_evidence stamp-payload.
	const file = manifest({ release_channel: undefined, evidence_written_at_utc: undefined });
	const run = stamp({ [CHANNEL]: 'beta', [EVIDENCE]: '2026-10-27T06:00:00Z' }, ['--manifest', file]);
	assert.equal(run.status, 0, run.stderr);
	const written = JSON.parse(fs.readFileSync(run.out, 'utf8')) as Record<string, string>;
	assert.equal(written.release_channel, 'beta');
	assert.equal(written.evidence_written_at_utc, '2026-10-27T06:00:00Z');
	const shipped = JSON.parse(fs.readFileSync(file, 'utf8')).identity as Record<string, string>;
	assert.equal(shipped.release_channel, 'beta', 'the packed manifest must agree with the shell stamp');
	assert.equal(shipped.evidence_written_at_utc, '2026-10-27T06:00:00Z');
	// No evidence variable: empty, as stamp-payload writes for an unset OPENDJ_EVIDENCE_WRITTEN_AT_UTC.
	const noEvidence = stamp({ [CHANNEL]: '' }, ['--manifest', manifest({ release_channel: undefined, evidence_written_at_utc: undefined })]);
	assert.equal(noEvidence.status, 0, noEvidence.stderr);
});

test('stamp-build refuses a manifest without a channel when OPENDJ_BUILD_CHANNEL is unset, and writes nothing', () => {
	const file = manifest({ release_channel: undefined, evidence_written_at_utc: undefined });
	const before = fs.readFileSync(file, 'utf8');
	const run = stamp({}, ['--manifest', file]);
	assert.notEqual(run.status, 0);
	assert.ok(run.stderr.includes(CHANNEL), run.stderr);
	assert.equal(fs.existsSync(run.out), false);
	assert.equal(fs.readFileSync(file, 'utf8'), before, 'a refused stamp must not edit the manifest');
});

test('stamp-build refuses a manifest with only one of the two release fields, and leaves it untouched', () => {
	for (const partial of [{ release_channel: undefined }, { evidence_written_at_utc: undefined }]) {
		const file = manifest(partial);
		const before = fs.readFileSync(file, 'utf8');
		const run = stamp({ [CHANNEL]: 'stable', [EVIDENCE]: '2026-10-27T06:00:00Z' }, ['--manifest', file]);
		assert.notEqual(run.status, 0, JSON.stringify(partial));
		assert.match(run.stderr, /only one of release_channel and evidence_written_at_utc/);
		assert.equal(fs.readFileSync(file, 'utf8'), before);
	}
});

test('stamp-build validates every manifest field before writing the release fields back', () => {
	const file = manifest({ release_channel: undefined, evidence_written_at_utc: undefined, built_at_utc: null });
	const before = fs.readFileSync(file, 'utf8');
	const run = stamp({ [CHANNEL]: 'beta' }, ['--manifest', file]);
	assert.notEqual(run.status, 0);
	assert.equal(fs.readFileSync(file, 'utf8'), before, 'a refused stamp must not have edited the payload manifest');
	assert.equal(fs.existsSync(run.out), false);
});

test('stamp-build refuses a short sha that is not a prefix of the full sha', () => {
	const run = stamp({}, ['--manifest', manifest({ git_sha: 'deadbeef' })]);
	assert.notEqual(run.status, 0);
	assert.ok(run.stderr.includes('deadbeef') && run.stderr.includes(HEAD), run.stderr);
	assert.equal(fs.existsSync(run.out), false);
	// Control: the 8-character prefix build_engine_payload writes is accepted (the main manifest test).
});

test('stamp-build leaves the manifest unchanged when the stamp file cannot be written', () => {
	const file = manifest({ release_channel: undefined, evidence_written_at_utc: undefined });
	const before = fs.readFileSync(file, 'utf8');
	// The output path's parent is a regular file, so creating the stamp fails.
	const blocker = path.join(scratchDir('blocked-out'), 'not-a-dir');
	fs.writeFileSync(blocker, '');
	const out = path.join(blocker, 'build-identity.json');
	const base = { ...process.env };
	delete base[LANE];
	delete base[EVIDENCE];
	const result = spawnSync(process.execPath, [SCRIPT, out, '--manifest', file], {
		encoding: 'utf8',
		env: { ...base, [CHANNEL]: 'beta' }
	});
	assert.notEqual(result.status, 0);
	assert.notEqual(result.status, null);
	assert.equal(fs.readFileSync(file, 'utf8'), before, 'a failed stamp write must not leave the manifest edited');
});

test('stamp-build keeps a pre-existing stamp when the manifest cannot be updated', (t) => {
	const file = manifest({ release_channel: undefined, evidence_written_at_utc: undefined });
	const manifestDir = path.dirname(file);
	const before = fs.readFileSync(file, 'utf8');
	const out = path.join(scratchDir('prior-stamp'), 'build-identity.json');
	fs.writeFileSync(out, 'prior stamp\n');
	fs.chmodSync(manifestDir, 0o555); // the release fields cannot be written back
	try {
		// Root, and Windows ACLs, ignore this mode, so the failure this test needs cannot be produced there.
		const probe = path.join(manifestDir, 'write-probe');
		let enforced = false;
		try {
			fs.writeFileSync(probe, '');
			fs.rmSync(probe);
		} catch {
			enforced = true;
		}
		if (!enforced) {
			t.skip('a read-only directory is still writable by this user (root or no POSIX modes), so the manifest write cannot be made to fail');
			return;
		}
		const base = { ...process.env };
		delete base[LANE];
		delete base[EVIDENCE];
		const result = spawnSync(process.execPath, [SCRIPT, out, '--manifest', file], {
			encoding: 'utf8',
			env: { ...base, [CHANNEL]: 'beta' }
		});
		assert.notEqual(result.status, 0);
		assert.notEqual(result.status, null);
	} finally {
		fs.chmodSync(manifestDir, 0o755);
	}
	assert.equal(fs.readFileSync(out, 'utf8'), 'prior stamp\n', 'a failed run must not destroy the previous stamp');
	assert.equal(fs.readFileSync(file, 'utf8'), before);
	assert.deepEqual(fs.readdirSync(manifestDir), ['manifest.json'], 'no temp file may be left beside the manifest');
});

// ----- post-pack check ----------------------------------------------------------------------
function verify(payloadDir: string, stampFile: string): { status: number | null; out: string } {
	const r = spawnSync(process.execPath, [VERIFY, payloadDir, stampFile], { encoding: 'utf8' });
	return { status: r.status, out: r.stdout + r.stderr };
}

test('verify-pack passes when the packed payload names the stamped commit, and fails when it does not', () => {
	const manifestFile = manifest({});
	const good = stamp({}, ['--manifest', manifestFile]);
	assert.equal(verify(path.dirname(manifestFile), good.out).status, 0);
	const otherPayload = path.dirname(manifest({ git_sha_full: 'e'.repeat(40) }));
	const bad = verify(otherPayload, good.out);
	assert.equal(bad.status, 1);
	assert.ok(bad.out.includes('e'.repeat(40)), bad.out);
});

test('verify-pack reports UNKNOWN only for the tracked placeholder payload, and fails when there is no payload at all', () => {
	const checkoutStamp = stamp({ [LANE]: '', [CHANNEL]: '' });
	const placeholder = scratchDir('placeholder-payload');
	fs.copyFileSync(path.join(__dirname, '..', '..', 'payload', 'README.md'), path.join(placeholder, 'README.md'));
	const unknown = verify(placeholder, checkoutStamp.out);
	assert.equal(unknown.status, 0, unknown.out);
	assert.match(unknown.out, /UNKNOWN/);
	assert.equal(verify(path.join(placeholder, 'absent'), checkoutStamp.out).status, 1);
});

test('verify-pack fails a lone README.md that is not the tracked placeholder', () => {
	const checkoutStamp = stamp({ [LANE]: '', [CHANNEL]: '' });
	const impostor = scratchDir('impostor-payload');
	fs.writeFileSync(path.join(impostor, 'README.md'), '# some other readme\n');
	const run = verify(impostor, checkoutStamp.out);
	assert.equal(run.status, 1, run.out);
	assert.match(run.out, /not the tracked placeholder/);
});

test('verify-pack fails a packed payload that lost its manifest, rather than calling it the placeholder', () => {
	const checkoutStamp = stamp({ [LANE]: '', [CHANNEL]: '' });
	const broken = scratchDir('manifestless-payload');
	fs.mkdirSync(path.join(broken, 'bin'));
	fs.writeFileSync(path.join(broken, 'bin', 'opendj-engine'), '#!/bin/sh\n');
	const run = verify(broken, checkoutStamp.out);
	assert.equal(run.status, 1, run.out);
	assert.match(run.out, /no manifest\.json/);
});
