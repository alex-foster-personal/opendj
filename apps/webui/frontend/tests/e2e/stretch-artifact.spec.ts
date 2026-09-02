/**
 * The Signalsmith worklet must reach its ready handshake from the BUILT
 * artifact, in every engine the app ships in.
 *
 * Regression for the fix in "load Signalsmith worklet from untransformed
 * package asset". Before it, `createSignalsmithStretch` had no `moduleUrl`, so
 * the package `Function.toString()`d its own bundled code into a Blob at run
 * time. In a production bundle that string is broken (esbuild lowers the
 * processor class field into a chunk-scope helper the blob never carries, so
 * the constructor throws inside the worklet with no `processorerror`, and
 * creation times out after 15s), and WKWebView refuses `blob:` URLs for
 * `audioWorklet.addModule` outright. Every dev-server suite passed throughout,
 * because Vite serves the package untransformed.
 *
 * Requirements (mini-PRD):
 *
 * - ✔︎ ✅ 🎯 The build emits exactly one raw package asset.
 *     [if] the `?url` import is removed [then] no asset is emitted and this
 *       spec is red at its first assertion
 *     [if] two copies are emitted [then] the app could load a stale one
 * - ✔︎ ✅ 🎯 A shipped chunk references that asset.
 *     [if] the adapter wiring is tree-shaken away [then] nothing points at the
 *       emitted file and the app falls back to the broken blob
 * - ✔︎ ✅ 🎯 The asset registers a working processor in chromium and webkit.
 *     [if] `addModule` rejects [then] no deck can ever load
 *     [if] the processor constructor throws [then] no ready message arrives
 *       within the handshake budget
 * - ✔︎ ✅ 🎯 The same asset imports as an ES module on the MAIN thread.
 *     The adapter reads its node factory from this file rather than bundling a
 *     second transformed copy of the package. `addModule` passing proves only
 *     the worklet side; the main-thread side is a separate loader with its own
 *     failure modes, so it gets its own assertion.
 *     [if] the file is served with a MIME type the module loader refuses
 *       [then] `import()` rejects and no deck can load
 *     [if] the package stops default-exporting the factory [then] the adapter
 *       throws before it ever reaches the worklet
 */
import { readFileSync, globSync } from 'node:fs';
import { basename, join } from 'node:path';

import { expect, test } from '@playwright/test';

import { STRETCH_ARTIFACT_BUILD_DIR } from './playwright.stretch-artifact.config';

/** The name the package registers its processor under. */
const PROCESSOR_NAME = 'signalsmith-stretch';

/** How long the worklet gets to post its first message. */
const HANDSHAKE_TIMEOUT_MS = 5_000;

const ASSET_DIRECTORY = '_app/immutable/assets';

/**
 * The single raw package file the build emitted, as a build-relative path.
 *
 * Read from disk rather than hardcoded: the name carries a content hash, so
 * pinning it would turn every unrelated package bump into a false failure.
 */
function resolveEmittedWorkletAsset(): string {
	const matches = globSync(`${ASSET_DIRECTORY}/SignalsmithStretch*.mjs`, {
		cwd: STRETCH_ARTIFACT_BUILD_DIR
	});
	expect(
		matches,
		`expected exactly one ${ASSET_DIRECTORY}/SignalsmithStretch*.mjs in the build; ` +
			'without it the package falls back to its Function.toString blob, which no ' +
			'production bundle and no WKWebView can load'
	).toHaveLength(1);
	return matches[0];
}

test.describe('signalsmith worklet asset', () => {
	test('the build emits exactly one raw package asset', () => {
		const asset = resolveEmittedWorkletAsset();
		expect(basename(asset)).toMatch(/^SignalsmithStretch\.[^.]+\.mjs$/);
	});

	test('a shipped chunk references the emitted asset', () => {
		const assetName = basename(resolveEmittedWorkletAsset());
		const chunks = globSync('_app/immutable/**/*.js', {
			cwd: STRETCH_ARTIFACT_BUILD_DIR
		});
		expect(chunks.length, 'the build emitted no javascript chunks at all').toBeGreaterThan(0);
		const referencing = chunks.filter((chunk) =>
			readFileSync(join(STRETCH_ARTIFACT_BUILD_DIR, chunk), 'utf8').includes(assetName)
		);
		expect(
			referencing,
			`no built chunk mentions ${assetName}, so the adapter's moduleUrl wiring did ` +
				'not survive bundling and the app still reaches for the blob'
		).not.toHaveLength(0);
	});

	test('the emitted asset is importable on the main thread and exports the node factory', async ({
		page
	}) => {
		// The main thread loads this same asset with a plain `import()` to get
		// the package's `createNode`, rather than bundling a second transformed
		// copy of the 114KB package (whose 86KB inlined WASM cost 39,441 bytes
		// gzip on /performance, and which the main thread never executes).
		// Two ways that arrangement can break without any other test noticing:
		// the file is served with a MIME type the module loader refuses, or the
		// package stops default-exporting the factory. Both are asserted here
		// because both make every deck load fail and nothing else covers them.
		const assetUrl = `/${resolveEmittedWorkletAsset()}`;
		await page.goto('/', { waitUntil: 'domcontentloaded' });

		const shape = await page.evaluate<
			{ hasDefault: boolean; defaultType: string },
			{ assetUrl: string }
		>(
			async ({ assetUrl: url }) => {
				const loaded = (await import(url)) as { default?: unknown };
				return {
					hasDefault: 'default' in loaded,
					defaultType: typeof loaded.default
				};
			},
			{ assetUrl }
		);

		expect(shape.hasDefault, `${assetUrl} has no default export`).toBe(true);
		expect(
			shape.defaultType,
			'the default export is not callable, so the adapter cannot create a node'
		).toBe('function');
	});

	test('the emitted asset registers a processor that reaches its ready handshake', async ({
		page
	}) => {
		const assetUrl = `/${resolveEmittedWorkletAsset()}`;
		await page.goto('/', { waitUntil: 'domcontentloaded' });

		const firstMessage = await page.evaluate<
			unknown[],
			{ assetUrl: string; processorName: string; timeoutMs: number }
		>(
			async ({ assetUrl: url, processorName, timeoutMs }) => {
				const context = new AudioContext();
				// A suspended context never runs the worklet's render thread, so
				// the handshake would time out for a reason unrelated to the asset.
				await context.resume();
				await context.audioWorklet.addModule(url);
				const node = new AudioWorkletNode(context, processorName, {
					numberOfInputs: 1,
					numberOfOutputs: 1,
					outputChannelCount: [2]
				});
				try {
					return await new Promise<unknown[]>((resolve, reject) => {
						const timer = setTimeout(
							() => reject(new Error(`no worklet message within ${timeoutMs}ms`)),
							timeoutMs
						);
						node.port.onmessage = (event: MessageEvent) => {
							clearTimeout(timer);
							resolve(event.data as unknown[]);
						};
					});
				} finally {
					node.disconnect();
					await context.close();
				}
			},
			{ assetUrl, processorName: PROCESSOR_NAME, timeoutMs: HANDSHAKE_TIMEOUT_MS }
		);

		expect(Array.isArray(firstMessage), 'the worklet posted a non-array message').toBe(true);
		expect(firstMessage[0]).toBe('ready');
	});
});
