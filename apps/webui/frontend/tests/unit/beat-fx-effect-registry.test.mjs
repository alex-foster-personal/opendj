// requirement: BEATFX-13753
// [if] a new effect file plus one index.ts line is not accepted by beat_fx [then] the registry path is broken
// [if] spiral is not refused naming "kernel not built" with state unchanged [then] the built flags lie
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { build } from './esbuild-tsconfig.mjs';
import { importBundledSource } from './import-bundled-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const ENTRY = 'tests/unit/fixtures/beat-fx-registry-entry.ts';
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
const THROWAWAY = fileURLToPath(new URL('./fixtures/beat-fx-throwaway-effect.ts', import.meta.url));
const ANCHOR = "import './time-effects';";
const NOT_BUILT = ['spiral', 'helix', 'reverse_delay', 'multitap', 'tape'];

let fx;

/** Bundle the entry with ONE extra registration line in index.ts, resolving './throwaway' to the fixture. */
async function loadWithOneRegistrationLine() {
	const plugin = {
		name: 'one-registration-line',
		setup(b) {
			b.onResolve({ filter: /^\.\/throwaway$/ }, () => ({ path: THROWAWAY }));
			b.onLoad({ filter: /beat-fx-effects[\\/]index\.ts$/ }, async (args) => {
				const source = await readFile(args.path, 'utf8');
				assert.ok(source.includes(ANCHOR), 'index.ts registration block moved');
				return {
					contents: source.replace(ANCHOR, `${ANCHOR}\nimport './throwaway';`),
					loader: 'ts'
				};
			});
		}
	};
	const result = await build({
		entryPoints: [fileURLToPath(new URL(`../../${ENTRY}`, import.meta.url))],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [plugin, viteUrlSuffixPlugin],
		target: 'node20',
		write: false
	});
	return importBundledSource(result.outputFiles[0].text, 'beat-fx-registry-throwaway');
}

function freshUnit(mod) {
	return { ...mod.DEFAULT_BEAT_FX_UNIT, host: null };
}

before(async () => {
	fx = await loadTypeScriptModule(ENTRY);
});

describe('beat FX effect registry', () => {
	test('one new effect file plus one index.ts line makes beat_fx accept the effect', async () => {
		// [if] a throwaway effect file plus one index.ts import is not accepted by parse and unit [then] registry path broken, [else stop].
		const mod = await loadWithOneRegistrationLine();
		assert.ok(mod.BEAT_FX_EFFECTS.includes('throwaway'));
		assert.ok(
			!fx.BEAT_FX_EFFECTS.includes('throwaway'),
			'stock registry must not carry the fixture'
		);
		const state = freshUnit(mod);
		const cmd = mod.parseBeatFxCommand({ type: 'beat_fx', effect: 'throwaway', enabled: true });
		mod.applyBeatFxUnitCommand(state, cmd, 0);
		assert.equal(state.effect, 'throwaway');
		assert.equal(state.host.effect, 'throwaway');
	});

	test('registering the same name twice throws naming it', () => {
		// [if] a second registerBeatFxEffect of echo does not throw with echo in the message [then] duplicates slip in, [else stop].
		assert.throws(
			() =>
				fx.registerBeatFxEffect({
					name: 'echo',
					levelLaw: 'balance',
					builtOnMain: true,
					assertDivisionAllowed: () => undefined,
					createHost: () => null
				}),
			/beat_fx effect echo is already registered/
		);
	});

	test('an effect with no kernel on main is refused by name and the unit state does not change', () => {
		// [if] beat_fx spiral (or another unbuilt row) is not refused naming it and kernel not built, state unchanged [then] flags lie, [else stop].
		for (const effect of NOT_BUILT) {
			const state = freshUnit(fx);
			const before = JSON.stringify(state);
			const cmd = fx.parseBeatFxCommand({ type: 'beat_fx', effect, enabled: true, target: '2' });
			assert.throws(
				() => fx.applyBeatFxUnitCommand(state, cmd, 0),
				new RegExp(`beat_fx effect ${effect} refused: kernel not built`)
			);
			assert.equal(JSON.stringify(state), before, effect);
		}
	});

	test('echo, delay and pingpong still select and host a kernel', () => {
		// [if] beat_fx effect echo, delay or pingpong is refused or hosts no kernel [then] the built effects regressed, [else stop].
		for (const effect of ['echo', 'delay', 'pingpong']) {
			const state = freshUnit(fx);
			fx.applyBeatFxUnitCommand(
				state,
				fx.parseBeatFxCommand({ type: 'beat_fx', effect, beats: 1 }),
				0
			);
			assert.equal(state.effect, effect);
			assert.equal(state.host.effect, effect);
			assert.equal(state.beats, 1);
		}
	});
});
