// export-midi-maps.mjs -- write the page's built-in MIDI device maps out as
// JSON for the Rust audio engine (plan 20-03), or check that the committed
// export still matches them.
//
// WHY THIS EXISTS
// The engine reads MIDI itself (odj-audio serve --midi) and must bind the
// same controls the page binds. The maps are TypeScript modules built with
// helper functions, so hand-copying their numbers into Rust would fork them
// the first time either side changes. Instead this script loads the real
// registry, DEVICE_MAP_REGISTRY in src/lib/rb/midi/maps/index.ts, through
// vite (the same way tests/unit/flx4-map.test.mjs loads it) and writes each
// DeviceMap verbatim. The engine embeds that file at compile time.
//
// USAGE
//   node scripts/export-midi-maps.mjs           write the export
//   node scripts/export-midi-maps.mjs --check   exit 1 when it is stale
//
// The check compares bytes, and refuses to call an empty registry in sync:
// a registry that failed to load must never render as "nothing to export".

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('..', import.meta.url));
export const EXPORT_PATH = resolve(FRONTEND_ROOT, '../../audio-engine/maps/device-maps.json');
const REGISTRY = '/src/lib/rb/midi/maps/index.ts';

/** Render the registry as the exact bytes the engine embeds. */
export async function renderExport() {
	const vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	try {
		const { DEVICE_MAP_REGISTRY } = await vite.ssrLoadModule(REGISTRY);
		if (!Array.isArray(DEVICE_MAP_REGISTRY) || DEVICE_MAP_REGISTRY.length === 0) {
			throw new Error(`${REGISTRY} exported no DEVICE_MAP_REGISTRY entries`);
		}
		const head = {
			generated_by: 'apps/webui/frontend/scripts/export-midi-maps.mjs',
			source: 'apps/webui/frontend/src/lib/rb/midi/maps/index.ts DEVICE_MAP_REGISTRY',
			note: 'Generated. Edit the TypeScript maps and rerun the script; never edit this file by hand.'
		};
		// One binding, LED rule or hint per line, so a map change reads as a
		// one-line diff here.
		const lines = ['{'];
		for (const [k, v] of Object.entries(head)) lines.push(` ${JSON.stringify(k)}: ${JSON.stringify(v)},`);
		lines.push(' "maps": [');
		DEVICE_MAP_REGISTRY.forEach((map, i) => {
			lines.push('  {');
			const keys = Object.keys(map);
			keys.forEach((k, j) => {
				const comma = j < keys.length - 1 ? ',' : '';
				const v = map[k];
				if (Array.isArray(v)) {
					lines.push(`   ${JSON.stringify(k)}: [`);
					v.forEach((x, n) => lines.push(`    ${JSON.stringify(x)}${n < v.length - 1 ? ',' : ''}`));
					lines.push(`   ]${comma}`);
				} else {
					lines.push(`   ${JSON.stringify(k)}: ${JSON.stringify(v)}${comma}`);
				}
			});
			lines.push(`  }${i < DEVICE_MAP_REGISTRY.length - 1 ? ',' : ''}`);
		});
		lines.push(' ]', '}');
		const text = lines.join('\n') + '\n';
		// The hand formatting must still be the same document.
		const round = JSON.parse(text);
		if (JSON.stringify(round.maps) !== JSON.stringify(DEVICE_MAP_REGISTRY)) {
			throw new Error('export formatting changed the maps; refusing to write it');
		}
		return text;
	} finally {
		await vite.close();
	}
}

async function main() {
	const check = process.argv.includes('--check');
	const want = await renderExport();
	if (!check) {
		writeFileSync(EXPORT_PATH, want);
		console.log(`wrote ${EXPORT_PATH}`);
		return;
	}
	let have;
	try {
		have = readFileSync(EXPORT_PATH, 'utf8');
	} catch (exc) {
		console.error(`missing ${EXPORT_PATH}: ${exc}`);
		process.exit(1);
	}
	if (have !== want) {
		console.error(
			`${EXPORT_PATH} is stale: the TypeScript device maps changed. Run node scripts/export-midi-maps.mjs`
		);
		process.exit(1);
	}
	console.log(`in sync: ${EXPORT_PATH}`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	await main();
}
