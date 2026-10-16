// The test bundler never reads tsconfig files (esbuild-tsconfig.mjs).
//
// [if] a tsconfig.json esbuild would read is empty, the state SvelteKit's
//   writer leaves while a concurrent test file's Vite server rewrites
//   .svelte-kit/tsconfig.json [then] a build given TSCONFIG_RAW still
//   compiles; the same build without it fails with the CI error (control)
// [if] any file under tests/unit, at any depth, imports from 'esbuild' in
//   any form other than named transform/transformSync imports (so it could
//   build without the pin) [then] this fails, naming the file
// [if] TSCONFIG_RAW drifts from the tsconfig chain on an option esbuild acts
//   on [then] this fails
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { build as unpinnedBuild } from 'esbuild';

import { TSCONFIG_RAW, build } from './esbuild-tsconfig.mjs';

const UNIT = fileURLToPath(new URL('.', import.meta.url));
const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

test('an empty tsconfig.json does not break a build given TSCONFIG_RAW; without it, it does', async () => {
	const dir = mkdtempSync(join(tmpdir(), 'esbuild-tsconfig-'));
	try {
		writeFileSync(join(dir, 'tsconfig.json'), '');
		writeFileSync(join(dir, 'entry.ts'), 'export class A { x = 1 }\nexport const n: number = new A().x;\n');
		const options = { absWorkingDir: dir, entryPoints: ['entry.ts'], bundle: true, write: false, format: 'esm', logLevel: 'silent' };
		await assert.rejects(unpinnedBuild(options), /tsconfig\.json:1:0: ERROR: Unexpected end of file in JSON/, 'control: the CI failure, reproduced');
		const result = await build(options);
		assert.match(result.outputFiles[0].text, /n = new A\(\)\.x/);
		assert.throws(() => build({ ...options, tsconfigRaw: {} }), /tsconfigRaw is pinned here/);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

// The two files allowed to import build from 'esbuild': the pinned wrapper,
// and this test, whose control builds without the pin on purpose.
const ALLOWED = new Set(['esbuild-tsconfig.mjs', 'esbuild-tsconfig.test.mjs']);
// transform/transformSync take source text and never read a tsconfig file.
const SAFE_NAMES = new Set(['transform', 'transformSync']);
const SAFE_IMPORT = /import\s*\{([^}]*)\}\s*from\s*['"]esbuild['"]/g;
const ANY_ESBUILD = /['"`]esbuild['"`]/g;

function* sourceFiles(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) yield* sourceFiles(path);
		else if (/\.(mjs|cjs|js|ts|mts|cts)$/.test(entry.name)) yield path;
	}
}

/** The file's references to the 'esbuild' module that are NOT a named import
 * of transform/transformSync. A whitelist, not a pattern for bad calls: an
 * import shape this does not recognize (namespace, default, dynamic import,
 * require, re-export, aliased build) is counted, so it fails loud. */
function unpinnedEsbuildReferences(source) {
	let safe = 0;
	for (const match of source.matchAll(SAFE_IMPORT)) {
		const names = match[1].split(',').map((name) => name.trim()).filter(Boolean);
		if (names.every((name) => SAFE_NAMES.has(name))) safe += 1;
	}
	return (source.match(ANY_ESBUILD)?.length ?? 0) - safe;
}

test('the reference scan counts every import shape that could build unpinned (control)', () => {
	for (const shape of [
		"import { build } from 'esbuild';",
		"import { build as b } from 'esbuild';",
		"import { transform, build } from 'esbuild';",
		"import * as esbuild from 'esbuild';",
		"import esbuild from 'esbuild';",
		"const { build } = await import('esbuild');",
		"const esbuild = require(\"esbuild\");",
		"export { build } from 'esbuild';"
	]) {
		assert.equal(unpinnedEsbuildReferences(shape), 1, shape);
	}
	assert.equal(unpinnedEsbuildReferences("import { transformSync } from 'esbuild';"), 0);
	assert.equal(unpinnedEsbuildReferences("import {\n\ttransform\n} from \"esbuild\";"), 0);
});

test('nothing under tests/unit builds with esbuild except through the pinned build', () => {
	const offenders = [];
	let scanned = 0;
	let pinnedUsers = 0;
	for (const path of sourceFiles(UNIT)) {
		const name = relative(UNIT, path);
		scanned += 1;
		const source = readFileSync(path, 'utf8');
		if (/from '\.\/esbuild-tsconfig\.mjs'/.test(source)) pinnedUsers += 1;
		if (ALLOWED.has(name)) continue;
		if (unpinnedEsbuildReferences(source) > 0) offenders.push(name);
	}
	assert.deepEqual(offenders, [], 'import build from ./esbuild-tsconfig.mjs, not from esbuild');
	// Presence, not just absence: the walk saw the tree and the bundlers that use the pin.
	assert.ok(scanned > 100, `the walk found ${scanned} source files under tests/unit`);
	assert.ok(pinnedUsers >= 8, `the walk found ${pinnedUsers} files building through the pin`);
});

/** A tsconfig file, re-read while another test file's Vite server may be
 * rewriting it (the race this file is about); a file that never parses fails. */
function readJson(path) {
	let last;
	for (let attempt = 0; attempt < 200; attempt += 1) {
		const text = readFileSync(path, 'utf8');
		try {
			return JSON.parse(text);
		} catch (exc) {
			last = exc;
			Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
		}
	}
	throw new Error(`${path} never parsed: ${last}`);
}

test('TSCONFIG_RAW matches the tsconfig chain on every option it sets', () => {
	const root = readJson(join(FRONTEND, 'tsconfig.json'));
	assert.equal(root.extends, './.svelte-kit/tsconfig.json');
	const kit = readJson(join(FRONTEND, '.svelte-kit/tsconfig.json'));
	const effective = { ...kit.compilerOptions, ...root.compilerOptions };
	for (const [key, value] of Object.entries(TSCONFIG_RAW.compilerOptions)) {
		assert.equal(effective[key], value, `compilerOptions.${key}`);
	}
	// esbuild also acts on these; none is set in the chain, so none is pinned.
	for (const key of ['useDefineForClassFields', 'experimentalDecorators', 'preserveValueImports', 'importsNotUsedAsValues', 'jsx', 'alwaysStrict']) {
		assert.equal(effective[key], undefined, `the chain now sets ${key}; pin it in TSCONFIG_RAW`);
	}
});
