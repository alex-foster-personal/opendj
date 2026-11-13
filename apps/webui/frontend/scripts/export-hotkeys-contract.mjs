// export-hotkeys-contract.mjs -- write the hotkeys overlay's rows as JSON for
// `opendj hotkeys` (#12287), or `--check` that the committed export matches.
// It loads the real registries through vite (as export-midi-maps.mjs does),
// composes rows as HotkeysOverlay.svelte does and renders each per platform
// with the overlay's own formatter, so Python never re-types a chord or the
// Cmd/Ctrl rule. navigator.platform is pinned to Apple while loading: the
// assistant label resolves at import, and the export must not depend on host.

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('..', import.meta.url));
export const EXPORT_PATH = resolve(FRONTEND_ROOT, '../../opendj_cli/hotkeys_contract.json');
const HOTKEYS = '/src/lib/components/rb/hotkeys/hotkeys-registry.ts';
const COMMANDS = '/src/lib/keyboard/command-registry.ts';
const SOURCE_PLATFORM = 'MacIntel';

function _viteServer() {
	return createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		// SSR loading needs no browser dep pre-bundle; its scan is cancelled on close and logs an error.
		optimizeDeps: { noDiscovery: true, include: [] },
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
}

/** Load both registries with navigator.platform pinned, restoring the real one after. */
async function _loadRows() {
	const realNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', {
		value: { platform: SOURCE_PLATFORM },
		configurable: true
	});
	const vite = await _viteServer();
	try {
		const hotkeys = await vite.ssrLoadModule(HOTKEYS);
		const { listPerformanceCommands } = await vite.ssrLoadModule(COMMANDS);
		const entries = hotkeys.overlayHotkeyEntries(listPerformanceCommands());
		return {
			rows: hotkeys.hotkeyContractRows(entries),
			platforms: hotkeys.HOTKEY_CONTRACT_PLATFORMS
		};
	} finally {
		await vite.close();
		if (realNavigator) Object.defineProperty(globalThis, 'navigator', realNavigator);
		else delete globalThis.navigator;
	}
}

/** Render the overlay rows as the exact bytes the CLI reads. */
export async function renderExport() {
	const { rows, platforms } = await _loadRows();
	if (!Array.isArray(rows) || rows.length === 0) {
		throw new Error(`${HOTKEYS} produced no overlay rows`);
	}
	const head = {
		generated_by: 'apps/webui/frontend/scripts/export-hotkeys-contract.mjs',
		source: `apps/webui/frontend${HOTKEYS} overlayHotkeyEntries(listPerformanceCommands())`,
		note: 'Generated. Edit the TypeScript registry and rerun the script; never edit this file by hand.',
		platforms
	};
	// One row per line, so a registry change reads as a one-line diff here.
	const lines = ['{'];
	for (const [k, v] of Object.entries(head))
		lines.push(` ${JSON.stringify(k)}: ${JSON.stringify(v)},`);
	lines.push(' "rows": [');
	rows.forEach((row, i) => lines.push(`  ${JSON.stringify(row)}${i < rows.length - 1 ? ',' : ''}`));
	lines.push(' ]', '}');
	const text = lines.join('\n') + '\n';
	if (JSON.stringify(JSON.parse(text).rows) !== JSON.stringify(rows)) {
		throw new Error('export formatting changed the rows; refusing to write it');
	}
	return text;
}

async function main() {
	const want = await renderExport();
	if (!process.argv.includes('--check')) {
		writeFileSync(EXPORT_PATH, want);
		console.log(`wrote ${EXPORT_PATH}`);
	} else if (readFileSync(EXPORT_PATH, 'utf8') !== want) {
		console.error(`${EXPORT_PATH} is stale: run node scripts/export-hotkeys-contract.mjs`);
		process.exit(1);
	} else {
		console.log(`in sync: ${EXPORT_PATH}`);
	}
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	await main();
}
