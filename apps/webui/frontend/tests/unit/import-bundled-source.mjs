/**
 * Import an esbuild bundle produced in memory (`write: false`) as an ES module.
 *
 * The bundle used to be imported as a `data:text/javascript;base64,...` URL.
 * That works, but V8 names every stack frame after its module URL, so one
 * console.error of an Error thrown inside a bundled module printed the whole
 * base64 bundle (about 160 KB) per frame: the frontend unit job's log reached
 * 26 MB on PR #3732 (Mon 21 Sep 2026), 162 such lines, and the job hit its
 * 15 min timeout on a slow runner while streaming it. A file in the OS temp
 * dir gives V8 a short path instead, and Node resolves the bundle's remaining
 * `node:` builtins exactly as before (everything else is bundled, which the
 * data: URL already required, since a data: module cannot resolve bare
 * specifiers).
 *
 * One file per import, never reused: the ESM cache is keyed by URL, and a test
 * that bundles the same module twice (fresh module state per test) must get a
 * fresh evaluation each time, which the `#sequence` fragment on the data: URL
 * used to guarantee. The file is unlinked once the import has evaluated (Node
 * has read it by then) and the directory is removed at process exit.
 */
import { mkdtempSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

let bundleDir = null;
let sequence = 0;

/** @param {string} text bundled ESM source
 *  @param {string} stem a readable name for the file, e.g. the module's path
 *  @returns {Promise<Record<string, unknown>>} the module namespace */
export async function importBundledSource(text, stem = 'bundle') {
	if (bundleDir === null) {
		bundleDir = mkdtempSync(join(tmpdir(), 'mdt-unit-bundle-'));
		process.on('exit', () => rmSync(bundleDir, { recursive: true, force: true }));
	}
	sequence += 1;
	const safeStem = stem.replace(/[^A-Za-z0-9_.-]+/g, '_');
	const file = join(bundleDir, `${safeStem}.${sequence}.mjs`);
	writeFileSync(file, text);
	try {
		return await import(pathToFileURL(file).href);
	} finally {
		unlinkSync(file);
	}
}
