import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { before, test } from 'node:test';

import { canonicalRepoIdentity } from './git-remote-identity.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

// Both functions under test guard the ledger UI that FANOUT-CONVENTIONS.md
// tells every fleet agent to work from, and neither had a single test before
// Tue 1 Sep 2026. Both had shipped defects that a test would have caught on
// the day they landed.

let types;

before(async () => {
	types = await loadTypeScriptModule('src/routes/progress-tree/types.ts');
});

// The public repository the shipped UI links to. A checkout of the private
// source repository has a different origin by design, so origin alone cannot
// be the invariant; the constant must name origin OR this public repository.
const PUBLIC_REPO_IDENTITY = 'github.com/alex-foster-personal/opendj';

test('GITHUB_REPO_BASE names the origin remote or the public repository', () => {
	// INVARIANT, not a pinned value. The constant was seeded by hand from
	// origin in July with a comment saying to re-resolve it if origin moved.
	// Origin moved, nobody re-resolved, and every issue/PR chip in the ledger
	// pointed at a dead org. Comparing against the live remote cannot go
	// stale the way the hardcoded value did.
	//
	// Compared as repo IDENTITY, not as strings. The transport in the origin
	// URL is a property of THIS CHECKOUT, not of the repo: fleet machines
	// clone over SSH and get `git@github.com:owner/repo.git` for the very
	// same repo. Comparing literally failed every SSH clone (issue #714) and
	// said nothing about whether the links were correct.
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
	const base = canonicalRepoIdentity(types.GITHUB_REPO_BASE);
	assert.ok(
		base === canonicalRepoIdentity(remote) || base === PUBLIC_REPO_IDENTITY,
		`GITHUB_REPO_BASE (${base}) is neither origin (${canonicalRepoIdentity(remote)}) nor ${PUBLIC_REPO_IDENTITY}`
	);
});

/** Why this value cannot serve as the chip base, or null if it can.
 *
 * A predicate rather than three inline assertions, so each rule can be
 * exercised on a value that SHOULD fail it. An assertion that only ever runs
 * against the real constant passes whether or not it was written correctly,
 * which is the same check-that-cannot-go-red this file exists to remove.
 *
 * @param {string} value
 * @returns {string | null}
 */
function unusableAsChipBase(value) {
	if (!/^https:\/\/[^/]+\/[^/]+\/[^/]+$/.test(value)) {
		return 'not an https://host/owner/repo URL';
	}
	let parsed;
	try {
		parsed = new URL(value);
	} catch {
		return 'not a URL a browser can open';
	}
	// Credentials are the realistic way this constant acquires a secret: the
	// repo moves, someone re-resolves it by copying the HTTPS remote from a
	// machine that clones with a token, and
	// `https://x-access-token:ghs_...@host/owner/repo` satisfies every other
	// rule here. Identity parsing discards userinfo by design and `new URL()`
	// accepts it, so nothing else in this file would notice. The token would
	// be committed into the constant and pasted into every chip href the
	// ledger renders. Codex found it on #720.
	if (parsed.username || parsed.password) {
		return 'carries embedded credentials';
	}
	return null;
}

test('GITHUB_REPO_BASE stays a credential-free URL a browser can open', () => {
	// The other half of the invariant. Identity alone would accept
	// `git@github.com:owner/repo` in the constant - the correct repo, in a
	// form no chip href can navigate to. This is the guard against "fixing"
	// an identity mismatch by pasting the raw remote in.
	assert.equal(unusableAsChipBase(types.GITHUB_REPO_BASE), null);

	// CONTROLS: the same predicate on values that must be refused, so this
	// test reports on the rules and not merely on today's constant.
	assert.equal(
		unusableAsChipBase('git@github.com:private_owner/music-dj-tools'),
		'not an https://host/owner/repo URL'
	);
	assert.equal(
		unusableAsChipBase(
			'https://x-access-token:ghs_redacted@github.com/private_owner/music-dj-tools'
		),
		'carries embedded credentials'
	);
});

test('githubIssueUrl builds a URL under the real origin', () => {
	assert.equal(
		types.githubIssueUrl('677'),
		'https://github.com/alex-foster-personal/opendj/issues/677'
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
