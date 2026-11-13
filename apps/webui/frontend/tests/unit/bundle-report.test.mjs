// Unit tests for the per-module bundle report (scripts/bundle-report.mjs, vite-bundle-report.ts).
//
// Built on a synthetic SvelteKit output tree, the bundle-budget.test.mjs fixture pattern, with no
// real `pnpm build`.
//
// Regression lines:
// - if the per-surface module estimates stop adding up to the gate's surface total then the report
//   is reading a different set of files than the budget it is meant to explain
// - if a module in two chunks is summed into one row then a payback is chosen from a number no
//   chunk actually ships
// - if the plugin writes anything with BUNDLE_REPORT unset then a normal build changes
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, test } from 'node:test';
import { surfaceModules } from '../../scripts/bundle-report.mjs';
import { bundleReportPlugin, bundleReportRows } from '../../vite-bundle-report.ts';

const REPORT = fileURLToPath(new URL('../../scripts/bundle-report.mjs', import.meta.url));
const roots = [];
after(() => roots.forEach((r) => rmSync(r, { recursive: true, force: true })));

/** Make a temp dir that is removed after the run. */
function _tmp(prefix) {
	const dir = mkdtempSync(join(tmpdir(), prefix));
	roots.push(dir);
	return dir;
}

/** A miniature static build: library, /performance (two chunks sharing a module) and one lazy route. */
function _fixture() {
	const root = _tmp('bundle-report-');
	const imm = join(root, 'build/_app/immutable');
	for (const dir of ['entry', 'nodes', 'chunks']) mkdirSync(join(imm, dir), { recursive: true });
	mkdirSync(join(root, '.svelte-kit/generated/client'), { recursive: true });
	const write = (rel, body) => writeFileSync(join(imm, rel), body);
	write('chunks/shared.js', 'export const s=1;');
	write('chunks/lib.js', `import"./shared.js";export const l="${'library '.repeat(40)}";`);
	write(
		'chunks/perf.js',
		`import"./shared.js";export const p="${'performance deck '.repeat(60)}";`
	);
	write('chunks/perf2.js', `import"./perf.js";export const q="${'waveform ruler '.repeat(30)}";`);
	write('chunks/other.js', 'import"./shared.js";export const o=1;');
	write('entry/start.AAAAAAAA.js', 'import"../chunks/shared.js";');
	write(
		'entry/app.BBBBBBBB.js',
		'import"../chunks/shared.js";' +
			['0', '1', '2', '3', '6'].map((n) => `import("../nodes/${n}.CCCCCCCC.js");`).join('')
	);
	write('nodes/0.CCCCCCCC.js', 'import"../chunks/shared.js";');
	write('nodes/1.CCCCCCCC.js', 'import"../chunks/shared.js";');
	write('nodes/2.CCCCCCCC.js', 'import"../chunks/lib.js";');
	write('nodes/3.CCCCCCCC.js', 'import"../chunks/other.js";');
	write('nodes/6.CCCCCCCC.js', 'import"../chunks/perf.js";import"../chunks/perf2.js";');
	writeFileSync(
		join(root, 'build/index.html'),
		['entry/start.AAAAAAAA.js', 'entry/app.BBBBBBBB.js', 'nodes/0.CCCCCCCC.js', 'chunks/shared.js']
			.map((f) => `<link href="/_app/immutable/${f}" rel="modulepreload">`)
			.join('\n')
	);
	writeFileSync(
		join(root, '.svelte-kit/generated/client/app.js'),
		'export const dictionary = {\n\t\t"/": [2],\n\t\t"/sets": [3],\n\t\t"/performance": [6]\n\t};\n'
	);
	// Module rows as the plugin writes them; src/lib/util.ts sits in both /performance chunks.
	const rows = [
		{ chunk: 'chunks/perf.js', module: 'apps/webui/frontend/src/lib/deck.ts', rendered_bytes: 900 },
		{ chunk: 'chunks/perf.js', module: 'apps/webui/frontend/src/lib/util.ts', rendered_bytes: 100 },
		{
			chunk: 'chunks/perf2.js',
			module: 'apps/webui/frontend/src/lib/ruler.ts',
			rendered_bytes: 400
		},
		{
			chunk: 'chunks/perf2.js',
			module: 'apps/webui/frontend/src/lib/util.ts',
			rendered_bytes: 100
		},
		{
			chunk: 'chunks/lib.js',
			module: 'apps/webui/frontend/src/lib/library.ts',
			rendered_bytes: 300
		}
	];
	writeFileSync(join(root, '.svelte-kit/bundle-report.json'), JSON.stringify(rows));
	return root;
}

/** Three graph surfaces only, so the fixture needs no marker chunks. */
function _budgets() {
	const dir = _tmp('bundle-report-budgets-');
	const surfaces = {
		library: [10, 'library'],
		performance: [20, 'performance'],
		'other-lazy': [90, 'other']
	};
	for (const [name, [order, graph]] of Object.entries(surfaces)) {
		mkdirSync(join(dir, name, 'charges'), { recursive: true });
		const base = {
			note: name,
			order,
			attribution: { graph },
			limit: 20480,
			measured: 1000,
			measured_on: 'Tue 3 Nov 2026',
			baseline_sha: '7837e0961708e8b7a957258dbbcdbfdc3660a07a'
		};
		writeFileSync(join(dir, name, 'base.json'), JSON.stringify(base));
	}
	return dir;
}

/** Run the report CLI on a fixture and parse its --json output. */
function _report(root, budgets) {
	const out = execFileSync(
		process.execPath,
		[
			REPORT,
			'--root',
			root,
			'--budgets',
			budgets,
			'--surface',
			'performance',
			'--top',
			'25',
			'--json'
		],
		{ encoding: 'utf8' }
	);
	return JSON.parse(out);
}

test('lists /performance modules by estimated gzip bytes, summing to the gate total within 1 percent', () => {
	const report = _report(_fixture(), _budgets());
	const bytes = report.modules.map((m) => m.gzip_bytes);
	assert.deepEqual(
		bytes,
		[...bytes].sort((a, b) => b - a)
	);
	assert.equal(report.modules[0].module, 'apps/webui/frontend/src/lib/deck.ts');
	const sum = bytes.reduce((a, b) => a + b, 0);
	assert.ok(
		Math.abs(sum - report.total_gzip_bytes) <= report.total_gzip_bytes * 0.01,
		`${sum} vs ${report.total_gzip_bytes}`
	);
	assert.ok(
		!report.modules.some((m) => m.module.includes('library.ts')),
		'a library module leaked into /performance'
	);
});

test('a module in two chunks is listed once per chunk with the chunk named, never summed', () => {
	const report = _report(_fixture(), _budgets());
	const util = report.modules.filter((m) => m.module.endsWith('src/lib/util.ts'));
	assert.deepEqual(util.map((m) => m.chunk).sort(), ['chunks/perf.js', 'chunks/perf2.js']);
});

test('a chunk with no module rows still counts toward the surface as one named entry', () => {
	const files = [{ file: 'chunks/x.js', surface: 'performance', bytes: 50 }];
	assert.deepEqual(surfaceModules(files, [], 'performance'), [
		{ chunk: 'chunks/x.js', module: '(chunk without module rows)', gzip_bytes: 50 }
	]);
});

test('the plugin rows key chunks by their _app/immutable path and modules repo-relative', () => {
	const rows = bundleReportRows(
		[
			{
				fileName: '_app/immutable/chunks/a.js',
				modules: {
					'/Users/dev/repo/src/x.ts': { renderedLength: 12 },
					'/Users/dev/repo/src/y.ts': { renderedLength: 0 }
				}
			}
		],
		'/Users/dev/repo'
	);
	assert.deepEqual(rows, [{ chunk: 'chunks/a.js', module: 'src/x.ts', rendered_bytes: 12 }]);
});

test('with BUNDLE_REPORT unset the plugin writes nothing and leaves the bundle untouched', () => {
	const out = join(_tmp('bundle-report-off-'), 'bundle-report.json');
	const bundle = {
		'a.js': {
			type: 'chunk',
			fileName: 'a.js',
			code: 'x',
			modules: { '/r/a.ts': { renderedLength: 1 } }
		}
	};
	const before = JSON.stringify(bundle);
	const plugin = bundleReportPlugin('/r', out, false);
	plugin.configResolved({ build: { ssr: false } });
	plugin.generateBundle({}, bundle);
	assert.equal(existsSync(out), false);
	assert.equal(JSON.stringify(bundle), before);
	const on = bundleReportPlugin('/r', out, true);
	on.configResolved({ build: { ssr: false } });
	on.generateBundle({}, bundle);
	assert.deepEqual(JSON.parse(readFileSync(out, 'utf8')), [
		{ chunk: 'a.js', module: 'a.ts', rendered_bytes: 1 }
	]);
});
