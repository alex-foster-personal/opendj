// The measurement harness's identity: manifest.harness_digest, computed the
// same way (every top-level .py, .mjs and .lock in this directory, by name and the
// sha256 of its bytes, in sorted name order). The driver stamps it into
// driver.json; the ledger refuses or will not compare captures whose driver,
// sampler and analyzer stamps differ, so a delta is never the instrument.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export function harnessDigest(lab = HERE) {
	const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
	const h = crypto.createHash('sha256');
	const names = fs
		.readdirSync(lab)
		.filter((n) => /\.(py|mjs|lock)$/.test(n) && fs.statSync(path.join(lab, n)).isFile())
		.sort();
	for (const n of names) h.update(`${n}\0${sha(fs.readFileSync(path.join(lab, n)))}\n`);
	return h.digest('hex');
}

// The realized versions of the driver's own JavaScript measurement
// dependencies, as resolved from where the driver loads them (requireFrom is
// the driver's createRequire on the frontend's package.json). Playwright lives
// in that checkout's node_modules, not in this directory, so the source digest
// alone cannot say which Playwright drove a capture; the ledger folds these
// into the capture's harness identity, as it does sampler and analyzer
// libraries. playwright-core is resolved from playwright's own location, the
// copy it really loads.
export function jsMeasurementDeps(requireFrom) {
	// read from disk on every call, never through require's module cache: a
	// second stamp (harnessMoved) must see a Playwright replaced since the first
	const version = (req, name) => {
		try {
			return String(JSON.parse(fs.readFileSync(req.resolve(`${name}/package.json`), 'utf8')).version);
		} catch {
			return 'absent';
		}
	};
	// the installed bytes too (packageDigest): a pnpm patch or a rebuilt
	// install keeps the version string and changes what runs
	const digest = (req, name) => {
		try {
			return packageDigest(path.dirname(req.resolve(`${name}/package.json`)));
		} catch {
			return 'absent';
		}
	};
	let core = 'absent';
	let coreDigest = 'absent';
	try {
		const fromPlaywright = createRequire(requireFrom.resolve('playwright/package.json'));
		core = version(fromPlaywright, 'playwright-core');
		coreDigest = digest(fromPlaywright, 'playwright-core');
	} catch {
		// playwright itself is absent; recorded as such below
	}
	return {
		node: process.version,
		node_sha256: nodeSha256(),
		playwright: version(requireFrom, 'playwright'),
		'playwright-core': core,
		playwright_sha256: digest(requireFrom, 'playwright'),
		'playwright-core_sha256': coreDigest,
		instrument_env: instrumentEnv()
	};
}

// sha256 over a package's installed bytes: every file under its real
// directory (pnpm links resolved) by relative path, in sorted order, without
// its nested node_modules (another package, recorded on its own). Playwright's
// browsers.json, which pins the browser revision it launches, is one of them;
// the browser's own bytes are the stack manifest's browser_sha256, hashed in
// full at `stack.py up` and re-checked at each capture's start and end.
export function packageDigest(dir) {
	const root = fs.realpathSync(dir);
	const files = [];
	const walk = (rel) => {
		for (const e of fs.readdirSync(path.join(root, rel), { withFileTypes: true })) {
			const r = rel ? `${rel}/${e.name}` : e.name;
			if (e.isDirectory()) {
				if (e.name !== 'node_modules') walk(r);
			} else if (e.isFile() || e.isSymbolicLink()) files.push(r);
		}
	};
	walk('');
	const h = crypto.createHash('sha256');
	for (const r of files.sort()) {
		h.update(`${r}\0${crypto.createHash('sha256').update(fs.readFileSync(path.join(root, r))).digest('hex')}\n`);
	}
	return h.digest('hex');
}

// The identity a JS instrument stamps (manifest.harness_stamp): taken BEFORE
// it loads Playwright, and again just before it writes its result.
export function harnessStamp(requireFrom, lab = HERE) {
	return { harness: harnessDigest(lab), measurement_deps: jsMeasurementDeps(requireFrom) };
}

// Why the stamp taken after measuring is not the one taken before, or null
// (manifest.harness_moved). A result whose identity moved records none.
export function harnessMoved(before, after) {
	if (before.harness !== after.harness) return `the lab code changed while the instrument ran (${before.harness.slice(0, 12)} -> ${after.harness.slice(0, 12)})`;
	const a = before.measurement_deps;
	const b = after.measurement_deps;
	const moved = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k])).sort();
	return moved.length ? `the instrument's libraries or environment changed while it ran: ${moved.join(', ')}` : null;
}

// The environment variables that change how a measuring process itself runs,
// matched upper-cased: the same lists as envs.py INSTRUMENT_RECORDED,
// INSTRUMENT_RECORDED_PREFIXES and INSTRUMENT_NOT_RECORDED (one rule for the
// Node and the Python instruments; a test compares them). Only behavior is
// recorded, never a locator such as PATH, HOME, PWD or TMPDIR, which moves
// between shells without changing a measurement.
export const INSTRUMENT_ENV_NAMES = new Set([
	'UV_THREADPOOL_SIZE',
	'LD_PRELOAD',
	'LD_LIBRARY_PATH',
	'LD_BIND_NOW',
	'GLIBC_TUNABLES',
	'TZ',
	'LANG',
	'DEBUG',
	// envs.py INSTALL_DROPPED's runtime settings (the prefixes cover them too)
	'NODE_OPTIONS',
	'PYTHONPATH',
	'PYTHONHOME'
]);
export const INSTRUMENT_ENV_NOT_RECORDED = new Set(['PLAYWRIGHT_BROWSERS_PATH', 'NODE_EXTRA_CA_CERTS']);
export const INSTRUMENT_ENV_PREFIXES = [
	'NODE_',
	'PYTHON',
	'MALLOC',
	'DYLD_',
	'LC_',
	'JEMALLOC',
	'TCMALLOC',
	'OMP_',
	'OPENBLAS_',
	'MKL_',
	'PLAYWRIGHT_',
	'PW_'
];

/** The part of env that changes how this instrument runs, sorted by name
 * (code-unit order, as Python sorts). measurement_deps carries it into the
 * capture's harness identity, so captures whose driver ran under another
 * NODE_OPTIONS or MALLOC_ARENA_MAX never compare. */
export function instrumentEnv(env = process.env) {
	const keep = (k) => {
		const up = k.toUpperCase();
		if (INSTRUMENT_ENV_NOT_RECORDED.has(up)) return false;
		return INSTRUMENT_ENV_NAMES.has(up) || INSTRUMENT_ENV_PREFIXES.some((p) => up.startsWith(p));
	};
	return Object.fromEntries(
		Object.keys(env)
			.filter((k) => env[k] !== undefined && keep(k))
			.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
			.map((k) => [k, env[k]])
	);
}

// sha256 of the Node executable running this process, its real path resolved
// (the stack manifest's node_sha256 is computed the same way). Two Node
// binaries can report one process.version, and a runtime-induced delta must
// never be attributed to the application.
export function nodeSha256(exe = process.execPath) {
	return crypto.createHash('sha256').update(fs.readFileSync(fs.realpathSync(exe))).digest('hex');
}
