/** Canonical identity of a git remote, independent of how it is reached.
 *
 * `https://github.com/o/r.git`, `git@github.com:o/r` and
 * `ssh://git@github.com/o/r.git` are the same repo reached three ways. Tests
 * that assert repo identity must compare the identity, not the string: the
 * fleet clones over SSH and CI clones over HTTPS, so a literal comparison
 * asserts the clone convention of whoever wrote it. That is issue #714.
 *
 * Host is part of the identity - the same owner/repo slug exists on any
 * GitHub Enterprise install, and the links this guards are absolute URLs. */

// Two forms, because only ONE of them has ports. Git documents the SCP-like
// form as `[<user>@]<host>:<path-to-git-repo>` with no port anywhere in it, so
// in that form the colon introduces a PATH; ports live in the scheme-based URI
// form alone. Reading them as one shape with an optional port is what let
// `git@gitlab.com:443/owner/repo.git` return `gitlab.com/owner/repo`, a
// confident identity for a repo nobody named, while the same subgroup remote
// with a non-numeric first segment correctly threw. Codex found that on #720.
//
// URI form: scheme:// | user[:secret]@ | host | [':' port] | '/' | owner/repo
// The port group is why `ssh://git@github.com:443/o/r.git` parses at all: an
// SSH endpoint on 443 is the standard way through a firewall that blocks 22,
// and without the group the host capture stopped at the colon.
const URI_REMOTE =
	/^[a-z][a-z0-9+.-]*:\/\/(?:[^@/]+@)?([^/:]+)(?::\d+)?\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/i;

// SCP-like and bare-host forms: user@host:owner/repo, or host/owner/repo for a
// browser URL pasted without its scheme. No port group, so a numeric first
// path segment stays a path segment: `git@github.com:443/repo.git` is owner
// `443`, and `git@gitlab.com:443/owner/repo.git` is a three-segment path this
// module refuses rather than guesses at. Refusing is the stated contract - a
// normalizer that returns a plausible string for input it did not understand
// turns a broken remote into a passing test.
const PATH_REMOTE = /^(?:[^@/]+@)?([^/:]+)[:/]([^/]+)\/([^/]+?)(?:\.git)?\/?$/i;

/**
 * @param {string} remoteUrl any git remote URL or browser URL for the repo
 * @returns {string} lowercased `host/owner/repo`
 */
export function canonicalRepoIdentity(remoteUrl) {
	const trimmed = typeof remoteUrl === 'string' ? remoteUrl.trim() : null;
	const match = trimmed ? (trimmed.match(URI_REMOTE) ?? trimmed.match(PATH_REMOTE)) : null;
	if (!match) {
		throw new Error(`cannot parse a repo identity from remote ${JSON.stringify(remoteUrl)}`);
	}
	const [, host, owner, repo] = match;
	// GitHub treats owner and repo case-insensitively and redirects between
	// casings, so casing is not part of identity and must not fail a test.
	return `${host}/${owner}/${repo}`.toLowerCase();
}
