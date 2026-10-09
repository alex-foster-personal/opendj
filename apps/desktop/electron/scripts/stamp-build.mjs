// Writes dist/build-identity.json before electron-builder so a packaged shell
// is stamped. readBuildStamp in src/policy.ts drops empty strings.
//
// The lane label and release channel come from the caller's environment and
// from nowhere else, as they do for the Tauri build (the dmg recipe exports
// both before cargo runs). There is no default: a pack whose caller forgot
// them must not be stamped as some lane it was never built for. An empty
// value is a statement ("no lane", "no channel") and is accepted; an unset
// variable is a mistake and stops the pack.

import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const out = process.argv[2] ?? path.join(root, 'dist', 'build-identity.json');

const REQUIRED_ENV = { lane_label: 'OPENDJ_BUILD_LANE_LABEL', release_channel: 'OPENDJ_BUILD_CHANNEL' };

function git(args) {
	return execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
}

const missing = Object.values(REQUIRED_ENV).filter((name) => process.env[name] === undefined);
if (missing.length > 0) {
	console.error(
		`stamp-build: ${missing.join(' and ')} must be set (an empty value means none). ` +
			'Nothing was written. Example: OPENDJ_BUILD_LANE_LABEL=electron OPENDJ_BUILD_CHANNEL=dev pnpm pack:dir'
	);
	process.exit(1);
}

const sha = git(['rev-parse', 'HEAD']);
const dirty = git(['status', '--porcelain']) === '' ? '0' : '1';
const builtAt = new Date().toISOString();
const stamp = {
	git_sha: sha.slice(0, 12),
	git_sha_full: sha,
	git_branch: git(['rev-parse', '--abbrev-ref', 'HEAD']),
	git_dirty: dirty,
	built_at_utc: builtAt,
	lane_label: process.env[REQUIRED_ENV.lane_label],
	release_channel: process.env[REQUIRED_ENV.release_channel]
};
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, `${JSON.stringify(stamp, null, 2)}\n`);
console.log(`stamp-build wrote ${out} sha=${stamp.git_sha}`);
