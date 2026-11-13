// vite-bundle-report.ts -- per-module rendered bytes of every client chunk, for scripts/bundle-report.mjs.
//
// Off unless BUNDLE_REPORT=1, so a normal `pnpm build` emits exactly what it did before. When on,
// generateBundle walks Rollup's public OutputChunk.modules for the CLIENT build only and writes
// .svelte-kit/bundle-report.json as {chunk, module, rendered_bytes} rows. `chunk` is the path under
// _app/immutable/ (the gate's file key) and `module` is repo-relative. The file sits under
// .svelte-kit/ rather than build/ because adapter-static empties build/ after the client build.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, relative } from 'node:path';
import type { Plugin } from 'vite';

export interface BundleReportRow {
	chunk: string;
	module: string;
	rendered_bytes: number;
}

const IMMUTABLE_PREFIX = '_app/immutable/';

/** Turn a Rollup module id into a stable repo-relative path (virtual ids keep their name). */
export function repoRelativeModule(id: string, repoRoot: string): string {
	const clean = id.replace(/^\0/, '').split('?')[0];
	return isAbsolute(clean) ? relative(repoRoot, clean).split('\\').join('/') : clean;
}

/** Build the report rows from emitted chunks: one row per module per chunk, never merged. */
export function bundleReportRows(
	chunks: Array<{ fileName: string; modules: Record<string, { renderedLength: number }> }>,
	repoRoot: string
): BundleReportRow[] {
	const rows: BundleReportRow[] = [];
	for (const chunk of chunks) {
		const name = chunk.fileName.startsWith(IMMUTABLE_PREFIX)
			? chunk.fileName.slice(IMMUTABLE_PREFIX.length)
			: chunk.fileName;
		for (const [id, info] of Object.entries(chunk.modules)) {
			if (info.renderedLength > 0) {
				rows.push({
					chunk: name,
					module: repoRelativeModule(id, repoRoot),
					rendered_bytes: info.renderedLength
				});
			}
		}
	}
	return rows;
}

/** The Vite plugin: a no-op unless `enabled`, then writes the rows for the client build. */
export function bundleReportPlugin(repoRoot: string, outFile: string, enabled: boolean): Plugin {
	let ssr = false;
	return {
		name: 'open-dj-bundle-report',
		apply: 'build',
		configResolved(config) {
			ssr = Boolean(config.build.ssr);
		},
		generateBundle(_options, bundle) {
			if (!enabled || ssr) return;
			const chunks = Object.values(bundle).flatMap((item) => (item.type === 'chunk' ? [item] : []));
			mkdirSync(dirname(outFile), { recursive: true });
			writeFileSync(outFile, JSON.stringify(bundleReportRows(chunks, repoRoot), null, 1));
		}
	};
}

export const BUNDLE_REPORT_FILE = join('.svelte-kit', 'bundle-report.json');
