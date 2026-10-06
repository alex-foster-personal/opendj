import assert from 'node:assert/strict';
import { test } from 'node:test';

import { canonicalRepoIdentity } from './git-remote-identity.mjs';

// A remote URL carries two independent facts: WHICH repo it is, and HOW this
// particular checkout authenticates to it. Transport is a per-clone choice
// (the fleet clones over SSH, CI clones over HTTPS), so any assertion that
// wants repo identity has to discard transport first or it is really
// asserting "this machine cloned the way I did". See issue #714.

test('canonicalRepoIdentity reduces every origin transport to one identity', () => {
	const identity = 'github.com/private_owner/music-dj-tools';
	const equivalentRemotes = [
		'https://github.com/private_owner/music-dj-tools',
		'https://github.com/private_owner/music-dj-tools.git',
		'https://github.com/private_owner/music-dj-tools/',
		'http://github.com/private_owner/music-dj-tools.git',
		'https://x-access-token:ghs_redacted@github.com/private_owner/music-dj-tools.git',
		'git@github.com:private_owner/music-dj-tools.git',
		'git@github.com:private_owner/music-dj-tools',
		'ssh://git@github.com/private_owner/music-dj-tools.git',
		// A port is transport, like the scheme and the credentials beside it.
		// 443 is the real case: it is how an SSH clone gets through a firewall
		// that blocks 22, and Git's own URI form puts the port right there.
		'ssh://git@github.com:443/private_owner/music-dj-tools.git',
		'ssh://git@github.com:22/private_owner/music-dj-tools',
		'https://github.com:8443/private_owner/music-dj-tools.git',
		'git://github.com/private_owner/music-dj-tools.git',
		'GIT@GitHub.com:private_owner/Music-DJ-Tools.git'
	];
	for (const remote of equivalentRemotes) {
		assert.equal(canonicalRepoIdentity(remote), identity, remote);
	}
});

test('canonicalRepoIdentity keeps the host, so a fork or an Enterprise mirror is a different repo', () => {
	// Owner/repo alone is not identity: the same slug exists on any GitHub
	// Enterprise host, and the ledger's chip links are absolute URLs.
	assert.notEqual(
		canonicalRepoIdentity('git@github.example.com:private_owner/music-dj-tools.git'),
		canonicalRepoIdentity('git@github.com:private_owner/music-dj-tools.git')
	);
	assert.notEqual(
		canonicalRepoIdentity('git@github.com:someone-else/music-dj-tools.git'),
		canonicalRepoIdentity('git@github.com:private_owner/music-dj-tools.git')
	);
});

test('an all-digit owner is still an owner, not a port', () => {
	// The overshoot control for the port group, and the reason it is digits
	// only. `git@host:owner/repo` uses the same colon as a path separator, so
	// a port group that matched anything would swallow the owner and hand back
	// an identity for a different repo - silently, with no throw to notice.
	// This is the one remote where the two readings genuinely collide.
	assert.equal(
		canonicalRepoIdentity('git@github.com:443/music-dj-tools.git'),
		'github.com/443/music-dj-tools'
	);
	// And the same shape where the port reading IS correct, so the assertion
	// above cannot be satisfied by a parser that ignores ports altogether.
	assert.equal(
		canonicalRepoIdentity('ssh://git@github.com:443/443/music-dj-tools.git'),
		'github.com/443/music-dj-tools'
	);
});

test('a three-segment path throws rather than inventing an identity from it', () => {
	// This is the case a permissive port group gets WRONG rather than merely
	// fails on, and it is why the group is digits only. A GitLab subgroup
	// remote hands `(?::[^/]+)?` a parse that hangs together - port `group`,
	// owner `subgroup` - so it returns a confident identity for a repo nobody
	// named, with nothing to notice. Digits only cannot match, and this module
	// throws on what it does not understand by design.
	//
	// Found by mutation: widening the group left every other test in this file
	// green, including the all-digit-owner one above, because a two-segment
	// path is rescued by backtracking either way.
	assert.throws(
		() => canonicalRepoIdentity('git@gitlab.com:group/subgroup/music-dj-tools.git'),
		/cannot parse/
	);
	// CONTROL: the same host and repo WITHOUT the extra segment parses, so the
	// throw above is about the shape and not about gitlab.com.
	assert.equal(
		canonicalRepoIdentity('git@gitlab.com:group/music-dj-tools.git'),
		'gitlab.com/group/music-dj-tools'
	);
});

test('a numeric first segment in the SCP form is a path segment, not a port', () => {
	// The same three-segment shape as the test above, with digits in front.
	// Git documents the SCP-like form as `[<user>@]<host>:<path-to-git-repo>`
	// and gives it no port at all, so the colon here introduces a PATH. A
	// parser that reads `443` as a port turns a subgroup remote nobody named
	// into a confident `gitlab.com/owner/repo` - the one failure mode this
	// module exists to refuse, and the digits-only group above cannot catch it
	// because the segment really is digits. Codex found it on #720.
	assert.throws(
		() => canonicalRepoIdentity('git@gitlab.com:443/owner/music-dj-tools.git'),
		/cannot parse/
	);
	// CONTROL, and it is the whole reason the port group still exists: the
	// same remote in Git's scheme-based URI form, where 443 IS a port, must
	// still reduce to the identity underneath it. Without this the fix could
	// be "delete the port group" and every other test would stay green.
	assert.equal(
		canonicalRepoIdentity('ssh://git@gitlab.com:443/owner/music-dj-tools.git'),
		'gitlab.com/owner/music-dj-tools'
	);
});

test('canonicalRepoIdentity throws on a remote it cannot parse instead of guessing', () => {
	// Fail loudly. A normalizer that returns a plausible-looking string for
	// unparseable input turns a broken remote into a passing test.
	for (const junk of ['', 'not-a-remote', 'https://github.com/owner-with-no-repo']) {
		assert.throws(
			() => canonicalRepoIdentity(junk),
			/cannot parse/,
			`expected a throw for ${JSON.stringify(junk)}`
		);
	}
});
