// Writes dist/build-identity.json before electron-builder so a packaged shell
// is stamped. readBuildStamp in src/policy.ts drops empty strings.
//
// Two sources, never mixed (EL-17-F12):
//
// - A built payload (payload/manifest.json, or --manifest <path>): every field
//   comes from the manifest's identity block, as the Tauri dmg recipe stamps
//   its binary, so the shell cannot claim a different commit from the engine
//   it ships with. The manifest's commit must be this checkout's HEAD, or the
//   payload came from somewhere else and the stamp is refused. Lane and
//   channel variables are optional here; if set, they must agree with it.
// - No payload (the documented empty-placeholder `pack:dir`): the commit is
//   read from this checkout, and the lane label and release channel come from
//   the caller's environment and from nowhere else. There is no default: an
//   unset variable is a mistake and stops the pack; an empty value is a
//   statement ("no lane", "no channel") and is accepted.
//
// The stable-evidence time (EL-17-F4) comes from the manifest. A manifest
// straight from build_engine_payload has neither release field, so they are
// added from OPENDJ_BUILD_CHANNEL and OPENDJ_BUILD_EVIDENCE_AT_UTC first, as
// the dmg recipe's `scripts.stable_evidence stamp-payload` step does.

import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');

const REQUIRED_ENV = { lane_label: 'OPENDJ_BUILD_LANE_LABEL', release_channel: 'OPENDJ_BUILD_CHANNEL' };
const EVIDENCE_ENV = 'OPENDJ_BUILD_EVIDENCE_AT_UTC';
const MANIFEST_FIELDS = [
	'git_sha',
	'git_sha_full',
	'git_branch',
	'git_dirty',
	'built_at_utc',
	'lane_label',
	'release_channel',
	'evidence_written_at_utc'
];

//----- helpers -----------------------------------------------------------------
function fail(message) {
	console.error(`stamp-build: ${message}. Nothing was written.`);
	process.exit(1);
}

/** Absolute path of `name` on an absolute PATH entry, so a shadow in the working directory never runs (AGENTS.md). */
function resolveProgram(name) {
	const exts = process.platform === 'win32' ? (process.env.PATHEXT ?? '.EXE;.CMD;.BAT').split(';').filter(Boolean) : [''];
	for (const dir of (process.env.PATH ?? process.env.Path ?? '').split(path.delimiter)) {
		if (!dir || !path.isAbsolute(dir)) continue;
		for (const ext of exts) {
			const candidate = path.join(dir, name + ext);
			if (fs.statSync(candidate, { throwIfNoEntry: false })?.isFile()) return candidate;
		}
	}
	fail(`${name} not found on an absolute PATH entry`);
}

let gitPath = null;
function git(args) {
	gitPath ??= resolveProgram('git');
	return execFileSync(gitPath, args, { cwd: root, encoding: 'utf8' }).trim();
}

function parseArgs(argv) {
	const args = { out: path.join(root, 'dist', 'build-identity.json'), manifest: null };
	for (let i = 0; i < argv.length; i++) {
		if (argv[i] === '--manifest') args.manifest = argv[++i];
		else args.out = argv[i];
	}
	return args;
}

/** One field as the Tauri recipe stamps it: booleans as 1/0, null and absent refused. */
function manifestField(identity, field, manifestPath) {
	if (!(field in identity)) fail(`${manifestPath} identity has no ${field}; it has ${Object.keys(identity).sort().join(', ')}`);
	const value = identity[field];
	if (value === null) fail(`${manifestPath} identity.${field} is null; the payload builder must fail rather than stamp an unknown value`);
	if (typeof value === 'boolean') return value ? '1' : '0';
	return String(value);
}

/**
 * What `scripts.stable_evidence stamp-payload` does in the Tauri recipe: build_manifest() writes
 * neither release field, so a manifest with both absent gets them from the environment (the
 * channel variable must be set, empty means none; an unset evidence time is ""). A manifest with
 * only one of them is malformed. Returns the fields to add, or null when both are present.
 */
function releaseFieldsToAdd(identity, manifestPath) {
	const hasChannel = 'release_channel' in identity;
	const hasEvidence = 'evidence_written_at_utc' in identity;
	if (hasChannel && hasEvidence) return null;
	if (hasChannel !== hasEvidence) {
		fail(`${manifestPath} has only one of release_channel and evidence_written_at_utc; a payload manifest has both or neither`);
	}
	const channel = process.env[REQUIRED_ENV.release_channel];
	if (channel === undefined) {
		fail(`${manifestPath} has no release_channel or evidence_written_at_utc, so ${REQUIRED_ENV.release_channel} must be set (an empty value means none)`);
	}
	return { release_channel: channel, evidence_written_at_utc: process.env[EVIDENCE_ENV] ?? '' };
}

function stampFromManifest(manifestPath) {
	const body = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
	const identity = body.identity;
	if (identity === null || typeof identity !== 'object') fail(`${manifestPath} has no identity block`);
	const head = git(['rev-parse', 'HEAD']);
	if (identity.git_sha_full !== head) {
		fail(`payload manifest ${manifestPath} was built from ${identity.git_sha_full} but this checkout is at ${head}; rebuild the payload here`);
	}
	const added = releaseFieldsToAdd(identity, manifestPath);
	const merged = { ...identity, ...added };
	const stamp = Object.fromEntries(MANIFEST_FIELDS.map((field) => [field, manifestField(merged, field, manifestPath)]));
	if (stamp.git_sha.length < 7 || !stamp.git_sha_full.startsWith(stamp.git_sha)) {
		fail(`${manifestPath} identity.git_sha ${stamp.git_sha} is not a prefix of git_sha_full ${stamp.git_sha_full}`);
	}
	for (const [field, name] of Object.entries(REQUIRED_ENV)) {
		const given = process.env[name];
		if (given !== undefined && given !== stamp[field]) {
			fail(`${name}=${JSON.stringify(given)} disagrees with the payload manifest's ${field} ${JSON.stringify(stamp[field])}`);
		}
	}
	// Every refusal is behind us; main() writes the manifest only after the stamp is in place.
	const manifestUpdate = added === null ? null : { path: manifestPath, body: { ...body, identity: merged } };
	return { stamp, manifestUpdate };
}

function stampFromCheckout() {
	const missing = Object.values(REQUIRED_ENV).filter((name) => process.env[name] === undefined);
	if (missing.length > 0) {
		fail(
			`${missing.join(' and ')} must be set (an empty value means none). ` +
				'Example: OPENDJ_BUILD_LANE_LABEL=electron OPENDJ_BUILD_CHANNEL=dev pnpm pack:dir'
		);
	}
	const sha = git(['rev-parse', 'HEAD']);
	const stamp = {
		git_sha: sha.slice(0, 12),
		git_sha_full: sha,
		git_branch: git(['rev-parse', '--abbrev-ref', 'HEAD']),
		git_dirty: git(['status', '--porcelain']) === '' ? '0' : '1',
		built_at_utc: new Date().toISOString(),
		lane_label: process.env[REQUIRED_ENV.lane_label],
		release_channel: process.env[REQUIRED_ENV.release_channel]
	};
	if (process.env[EVIDENCE_ENV] !== undefined) stamp.evidence_written_at_utc = process.env[EVIDENCE_ENV];
	return { stamp, manifestUpdate: null };
}

/**
 * Replace every target or none of them. All temp files are written first (a failure there leaves
 * every target as it was); then they are renamed in order, and if a later rename fails the earlier
 * targets get their previous contents back. Targets: [{ file, text }].
 */
function replaceAllOrNone(targets) {
	const staged = [];
	try {
		for (const { file, text } of targets) {
			const tmp = `${file}.tmp-${process.pid}`;
			fs.writeFileSync(tmp, text);
			staged.push({ file, tmp, previous: fs.existsSync(file) ? fs.readFileSync(file) : null });
		}
	} catch (error) {
		for (const { tmp } of staged) fs.rmSync(tmp, { force: true });
		throw error;
	}
	const done = [];
	try {
		for (const entry of staged) {
			fs.renameSync(entry.tmp, entry.file);
			done.push(entry);
		}
	} catch (error) {
		for (const { file, previous } of done) {
			if (previous === null) fs.rmSync(file, { force: true });
			else fs.writeFileSync(file, previous);
		}
		for (const { tmp } of staged) fs.rmSync(tmp, { force: true });
		throw error;
	}
}

//----- main --------------------------------------------------------------------
const args = parseArgs(process.argv.slice(2));
const defaultManifest = path.join(root, 'payload', 'manifest.json');
if (args.manifest !== null && !fs.existsSync(args.manifest)) fail(`--manifest ${args.manifest} does not exist`);
const manifestPath = args.manifest ?? (fs.existsSync(defaultManifest) ? defaultManifest : null);
const { stamp, manifestUpdate } = manifestPath === null ? stampFromCheckout() : stampFromManifest(manifestPath);

// Both files change together or not at all, and a previous stamp survives a failed run.
const targets = [{ file: args.out, text: `${JSON.stringify(stamp, null, 2)}\n` }];
if (manifestUpdate !== null) targets.push({ file: manifestUpdate.path, text: `${JSON.stringify(manifestUpdate.body, null, 2)}\n` });
try {
	fs.mkdirSync(path.dirname(args.out), { recursive: true });
	replaceAllOrNone(targets);
} catch (error) {
	fail(`cannot write ${targets.map((t) => t.file).join(' and ')}: ${error.message}`);
}
console.log(`stamp-build wrote ${args.out} sha=${stamp.git_sha} source=${manifestPath ?? 'checkout'}`);
