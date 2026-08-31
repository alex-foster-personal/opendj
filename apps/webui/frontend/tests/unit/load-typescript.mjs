import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
let moduleSequence = 0;

/** Bundle one frontend TypeScript module in memory for deterministic unit
 * tests. This avoids Vite's long-lived dependency-optimizer handles. */
export async function loadTypeScriptModule(relativePath, { viteApiBase } = {}) {
	const absolutePath = fileURLToPath(new URL(`../../${relativePath}`, import.meta.url));
	const result = await build({
		entryPoints: [absolutePath],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			'$state': 'globalThis.__musicDjToolsTestState',
			'import.meta.env.VITE_API_BASE':
				viteApiBase === undefined ? 'undefined' : JSON.stringify(viteApiBase)
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		target: 'node20',
		write: false
	});
	if (result.outputFiles.length !== 1) {
		throw new Error(`expected one bundled output, got ${result.outputFiles.length}`);
	}
	// $state is an identity function under test. $state.snapshot must exist as
	// well or any module whose SETTERS run (rather than just its initializer)
	// dies on `$state.snapshot is not a function` - prefs._persist is the first
	// such case. Runes are compile-time in Svelte; plain structured values are
	// an honest stand-in for the deep-clone the real rune performs.
	globalThis.__musicDjToolsTestState = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	const source = Buffer.from(result.outputFiles[0].text).toString('base64');
	moduleSequence += 1;
	return import(`data:text/javascript;base64,${source}#${moduleSequence}`);
}
