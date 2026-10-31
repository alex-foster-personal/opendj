/**
 * Run the production AutoPlay arm against a live engine.
 *
 * stdin JSON: {"origin":"http://127.0.0.1:<port>","feed":["..."],"chart":["..."]}
 * stdout: the stable ids the engine stored, one JSON array.
 *
 * Importing this module does not read stdin. The body runs only when node is
 * started on this file, which is how the live engine test launches it.
 */
import { readFileSync, realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { loadTypeScriptModule } from '../unit/load-typescript.mjs';

/**
 * True when this file is the node entry point, not an import from a unit test.
 * An entry path that cannot be resolved throws: read as "imported", it would
 * skip `main` and exit 0 with no result.
 */
export function startedAsCli() {
	const entry = process.argv[1];
	if (entry === undefined) return false;
	return import.meta.url === pathToFileURL(realpathSync(entry)).href;
}

async function main() {
	const payload = JSON.parse(readFileSync(0, 'utf8'));
	const origin = payload.origin;
	const feed = payload.feed;
	const chart = payload.chart;
	const writer = payload.writer;
	if (typeof origin !== 'string' || !origin.startsWith('http://127.0.0.1:')) {
		console.error('origin must be the live engine on 127.0.0.1');
		process.exit(2);
	}
	if (!Array.isArray(feed) || !Array.isArray(chart) || chart.length === 0) {
		console.error('feed and chart must be stable_id arrays, and chart must be non-empty');
		process.exit(2);
	}
	if (typeof writer !== 'string' || writer === '') {
		console.error('writer must be the client id of the page that holds the mirror lease');
		process.exit(2);
	}

	const mod = await loadTypeScriptModule('tests/live/arm-autoplay-next-up.ts', {
		viteApiBase: origin
	});
	const stored = await mod.armChartedNextUp(feed, chart, writer);
	process.stdout.write(`${JSON.stringify(stored)}\n`);
}

if (startedAsCli()) {
	await main();
}
