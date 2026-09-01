import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Both functions under test guard the ledger UI that FANOUT-CONVENTIONS.md
// tells every fleet agent to work from, and neither had a single test before
// Tue 1 Sep 2026. Both had shipped defects that a test would have caught on
// the day they landed.

let types;

before(async () => {
	types = await loadTypeScriptModule('src/routes/progress-tree/types.ts');
});

test('GITHUB_REPO_BASE matches the actual origin remote', () => {
	// INVARIANT, not a pinned value. The constant was seeded by hand from
	// origin in July with a comment saying to re-resolve it if origin moved.
	// Origin moved, nobody re-resolved, and every issue/PR chip in the ledger
	// pointed at a dead org. Comparing against the live remote cannot go
	// stale the way the hardcoded value did.
	let remote;
	try {
		const root = execFileSync('git', ['rev-parse', '--show-toplevel'], {
			encoding: 'utf8'
		}).trim();
		remote = execFileSync('git', ['remote', 'get-url', 'origin'], {
			cwd: root,
			encoding: 'utf8'
		}).trim();
	} catch (err) {
		// A tool that cannot measure must not return a verdict. Fail loudly
		// rather than skip, so this never reads as a silent pass.
		assert.fail(`could not resolve origin to compare against: ${err.message}`);
	}

	assert.ok(remote.length > 0, 'origin remote resolved to an empty string');
	const expected = remote.replace(/\.git$/, '');
	assert.equal(types.GITHUB_REPO_BASE, expected);
});

test('githubIssueUrl builds a URL under the real origin', () => {
	assert.equal(
		types.githubIssueUrl('677'),
		'https://github.com/maintainer/music-dj-tools/issues/677'
	);
});

test('staleBuildLabel returns null only for a well-formed fresh stamp', () => {
	const now = new Date('2026-09-01T12:00:00Z');
	assert.equal(types.staleBuildLabel('2026-09-01T11:00:00Z', now), null);
});

test('staleBuildLabel labels an unstamped claim instead of hiding it', () => {
	// THE REGRESSION THIS FILE EXISTS FOR. Returning null here rendered no
	// stale tag on NodeRow, NodeDetail or DepGraph, so a claim with no
	// build.updated could never look stale and could never be taken over.
	// The 3h lease was unenforceable against exactly the malformed claims it
	// most needed to catch.
	const now = new Date('2026-09-01T12:00:00Z');
	assert.equal(types.staleBuildLabel(null, now), 'stale (unstamped)');
});

test('staleBuildLabel labels an unparseable stamp instead of hiding it', () => {
	const now = new Date('2026-09-01T12:00:00Z');
	assert.equal(types.staleBuildLabel('not-a-timestamp', now), 'stale (bad timestamp)');
});

test('staleBuildLabel reports hours past the 3h lease, then days', () => {
	const now = new Date('2026-09-01T12:00:00Z');
	assert.equal(types.staleBuildLabel('2026-09-01T08:00:00Z', now), 'stale 4h');
	assert.equal(types.staleBuildLabel('2026-08-30T12:00:00Z', now), 'stale 2d');
});
