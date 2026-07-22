import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
let moduleSequence = 0;

/** Bundle one frontend TypeScript module in memory for deterministic unit
 * tests. This avoids Vite's long-lived dependency-optimizer handles. */
export async function loadTypeScriptModule(relativePath) {
	const absolutePath = fileURLToPath(new URL(`../../${relativePath}`, import.meta.url));
	const result = await build({
		entryPoints: [absolutePath],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			'$state': 'globalThis.__musicDjToolsTestState',
			'import.meta.env.VITE_API_BASE': 'undefined'
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
	globalThis.__musicDjToolsTestState = (value) => value;
	const source = Buffer.from(result.outputFiles[0].text).toString('base64');
	moduleSequence += 1;
	return import(`data:text/javascript;base64,${source}#${moduleSequence}`);
}
