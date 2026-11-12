// perf-lab: print the Chromium a capture launches, for the stack manifest.
//
//   node scripts/perf/lab/browser-id.mjs [--chrome PATH]
// prints {"executable": ..., "version": ...} as one JSON line. The version is
// read from a real headless launch (browser.version()), the same string the
// driver records, so it is a measurement rather than a guess from a path.

import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { resolveChrome } from './capture-meta.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const { chromium } = createRequire(path.join(REPO, 'apps/webui/frontend/package.json'))('playwright');

const args = process.argv.slice(2);
if (args.length && !(args.length === 2 && args[0] === '--chrome')) throw new Error(`usage: browser-id.mjs [--chrome PATH], got ${args.join(' ')}`);
const executable = resolveChrome(chromium, args[1]);
const browser = await chromium.launch({ headless: true, executablePath: executable });
try {
	console.log(JSON.stringify({ executable, version: browser.version() }));
} finally {
	await browser.close();
}
