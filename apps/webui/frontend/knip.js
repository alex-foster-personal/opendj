// knip.js -- dead-code and unused-dependency config for the frontend workspace.
//
// This was knip.json until Wed 16 Sep 2026. It is JavaScript now for exactly one
// reason: `compilers` takes a FUNCTION, and the built-in `.svelte` compiler in
// knip 6.32.2 silently drops import specifiers. Everything else below is the
// former knip.json verbatim.
//
// WHY THE OVERRIDE EXISTS
// knip does not parse .svelte directly. Its Svelte plugin registers a compiler
// that regex-scrapes import statements out of <script> blocks and feeds the
// result to knip's own JS parser. That scraper (`importMatcher` in knip's
// dist/compilers/compilers.js) is:
//
//   /import(?:\s*\(\s*['"][^'"]+['"][^)]*\)|(?!\s*\()[^'"]+['"][^'"]+['"])/g
//
// with no identifier boundary on either side of the keyword, so it also fires on
// the letters "import" INSIDE an ordinary identifier. SetupOverlay.svelte declares
// importPct, importDenied and importRunning; each one matched, and the second
// alternative then swallowed text up to the next pair of quotes, emitting things
// like `importRunning = $derived(\n job !== null && !['succeeded';`. That is not
// valid JavaScript. knip's parser gives up there, and EVERY specifier after the
// first corrupted match is lost, including
// `await import('@tauri-apps/plugin-dialog')` further down the same file. knip
// then reported @tauri-apps/plugin-dialog as an unused dependency, which failed
// the quality ratchet on frontend.unused_deps.
//
// Measured on this tree, Wed 16 Sep 2026: 5 of 198 .svelte files produced
// extraction that does not parse (PreflightScreen, BrowserPanel, BuildIdentity,
// SetupOverlay, routes/performance/preload1/+page). Only SetupOverlay happened to
// lose a dependency edge; the other four were losing import edges silently. A gate
// that cannot read a file must not return a verdict on it, so this is repaired
// rather than suppressed with ignoreDependencies: if plugin-dialog ever does
// become genuinely unused, knip will still say so.
//
// THE FIX is knip's own regex plus the guards it is missing on both sides of the
// keyword. knip's sibling matcher for dynamic imports in TEMPLATES already carries
// the leading one, so this is the same correction applied to the script scanner.
// Template dynamic imports are scanned too, so behavior stays a superset of the
// built-in compiler's. The trailing guard also excludes `.`, because `import.meta`
// is a meta-property rather than a specifier; that is a second instance of the same
// defect, and it was corrupting two further files.
//
// REMOVE THIS OVERRIDE when knip ships boundary-aware matching upstream: delete
// `compilers`, and this file can go back to being knip.json. The guard against
// removing it by accident is tests/unit/knip-svelte-compiler.test.mjs, which
// asserts the compiler still recovers a dynamic import that sits after an
// identifier beginning with "import".

const SCRIPT_BLOCK = /<script\b((?:[^>"']|"[^"]*"|'[^']*')*)>([\s\S]*?)<\/script>/gi;
const STYLE_BLOCK = /<style\b((?:[^>"']|"[^"]*"|'[^']*')*)>([\s\S]*?)<\/style>/gi;
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const BLOCK_COMMENT = /\/\*[\s\S]*?\*\//g;
const LINE_COMMENT = /^[ \t]*\/\/.*$/gm;

// knip's importMatcher, with the boundaries it lacks: an identifier guard on each
// side of the keyword, and `.` in the trailing guard so `import.meta.env.X` is left
// alone (it is a meta-property, not a specifier, and matching it swallowed the rest
// of the file in BuildIdentity.svelte and routes/performance/preload1/+page.svelte).
//
// A static import is also anchored to the start of a line. Without that anchor the
// word "import" inside a string ("Could not import Chrome sign-ins: ${...}") matched
// as a static import and swallowed text up to the next pair of quotes, which broke
// SpotifySourcePanel.svelte the same way identifiers broke SetupOverlay. Static
// imports are statements, so a line-start anchor loses no real one; the dynamic
// form can sit mid-expression and keeps only the identifier guards.
const IMPORT_SPECIFIER =
	/^[ \t]*import(?![\w$.])(?!\s*\()[^'"]+['"][^'"]+['"]|(?<![.\w$])import(?![\w$.])\s*\(\s*['"][^'"]+['"][^)]*\)/gm;

// Templates can only ever carry the dynamic form, so the static alternative is
// deliberately absent here: it would match across markup and emit nonsense.
const TEMPLATE_DYNAMIC_IMPORT = /(?<![.\w$])import(?![\w$.])\s*\(\s*['"][^'"]+['"][^)]*\)/g;

function _matches(text, pattern) {
	const found = [];
	pattern.lastIndex = 0;
	let match;
	while ((match = pattern.exec(text))) found.push(match[0]);
	return found;
}

/** Emit only import statements, never whole script bodies: knip counts exports it
 * can see, and a Svelte component's `export`s are props rather than module exports,
 * so handing it the full body would invent unused-export findings. */
export function svelteImports(text) {
	const statements = [];
	SCRIPT_BLOCK.lastIndex = 0;
	let script;
	while ((script = SCRIPT_BLOCK.exec(text))) {
		const body = script[2].replace(BLOCK_COMMENT, '').replace(LINE_COMMENT, '');
		statements.push(..._matches(body, IMPORT_SPECIFIER));
	}
	const template = text
		.replace(SCRIPT_BLOCK, '')
		.replace(STYLE_BLOCK, '')
		.replace(HTML_COMMENT, '');
	statements.push(..._matches(template, TEMPLATE_DYNAMIC_IMPORT));
	return statements.join(';\n');
}

export default {
	entry: [
		'src/routes/**/+*.{js,ts,svelte}',
		'src/hooks.{client,server}.{js,ts}',
		'src/service-worker.{js,ts}',
		'tests/unit/**/*.test.mjs',
		'tests/live/stem-decode-harness-entry.ts',
		// Bundled by path and loaded into real Chromium by headphone-signal-meter-gate.test.mjs.
		'tests/unit/headphone-meter-browser-entry.mjs',
		// Live cue alignment probes are run directly (node tests/live/<probe>.mjs
		// --url ...) against real audio hardware, so each one is an entry point,
		// not a module something is expected to import.
		'tests/live/cue-{align,bridge}-*.mjs',
		// Same shape: the RESCUE-05 cross-origin sink probe (real outputs, real origins).
		'tests/live/rescue-output-device-origin.mjs',
		// Same shape: the Beat Sync phase-lock workload, run directly against a live
		// engine's real beatgrids (read-only).
		'tests/live/phase-lock-jitter-workload.mjs',
		// The live-engine AutoPlay arm. tests/health_lights/test_ahead_analysis_live_engine.py
		// runs the .mjs, which loads the .ts through the unit bundler. Neither is
		// imported by the app, so without an entry knip reports both as orphans.
		'tests/live/arm-autoplay-next-up.mjs',
		'tests/live/arm-autoplay-next-up.ts',
		// Direct real-runtime keyboard acceptance for focused controls (#5761).
		'tests/live/performance-focused-controls.mjs',
		// Same shape: the two-Chromium remote speaker loopback proof (REMOTE-SPK-8119g) and
		// the browser entry it bundles with esbuild.
		'tests/live/remote-speaker-{loopback.mjs,harness-entry.ts}',
		// Same shape: the stem-load-under-playback scorer, imported into an open
		// /performance page by a person or an agent (PERF-STEMDEC-04 rounds).
		'tests/live/stem-load-under-playback-scorer.js',
		// Same shape: the PVSNAP preview beat snap timing harness, run directly in
		// headless Chromium; its entry is what esbuild bundles into the page.
		'tests/live/preview-beat-snap-harness.mjs',
		'tests/live/preview-beat-snap-harness-entry.ts',
		'tests/unit/fixtures/**/*.ts',
		'tests/e2e/fixtures/**/*.ts',
		'tests/manual/wkwebview-spike/inject.mjs',
		'tests/e2e/**/*.spec.ts',
		'tests/e2e/**/*.config.ts',
		// Playwright loads this reporter from the CLI (`--reporter=...,./tests/e2e/
		// support/footprint-reporter.ts`) in the e2e gate, so no module imports it.
		'tests/e2e/support/footprint-reporter.ts',
		'playwright.config.ts',
		'vite.config.ts',
		'svelte.config.js',
		'webui-port-config.ts',
		'scripts/**/*.{js,ts,mjs}',
		// Keyframe catalogue for ControlExplainer demos; consumed by unit tests, not runtime UI.
		'src/lib/rb/explainer-demo-keyframes.ts',
		// W28 right-click permutation harness: renders (private path)/*.md for
		// tests/unit/context-menu-registry.test.mjs; never shipped in the UI.
		'src/lib/context-menu/permutations.ts',
		// K1 selector tree (#9393): pure spatial-navigation core, consumed by unit tests until
		// key binding (K3) wires it into the UI.
		'src/lib/keyboard/selector-tree.ts',
		// D3 commanded-move runner (#9454): driven by unit tests until digit commands (D2, #9453)
		// call it from the UI.
		'src/lib/rb/commanded-move-runner.svelte.ts',
		// D1 commanded-move planner (#9452): pure planner, consumed by unit tests until digit
		// commands (D2, #9453) and the D3 runner wire it into the UI.
		'src/lib/rb/commanded-move.ts',
		// #9456 transition presets: pure planner/runner until Cmd-K wires the registry.
		'src/lib/rb/transition-presets.ts',
		// #9456 preset.run.* commands until palette epic imports the registry.
		'src/lib/keyboard/command-registry.ts',
		// #10270 / #9804 NOISE kernel: unit tests until color-fx-graph splices it in.
		'src/lib/rb/color-fx-noise.ts',
		// Edge sampler client (#10778), search, sets and store logic (#11102) and store core
		// (#10784): the store imports each module now, but part of each module's exports stay
		// unit-test-only until the edge components and agent parity land (port map rows 22-27).
		'src/lib/player/sampler/{sampler-types,sampler-repeat,api-sampler}.ts',
		'src/lib/player/sampler/{edge-search,sampler-sets,sampler-store-logic}.ts',
		'src/lib/player/sampler/sampler-store.svelte.ts',
		// Edge sampler engine sub-chain (#10782 warm pool, #11127 capture vector): pure modules
		// loaded only by unit tests until the engine (port map row 18) imports them.
		'src/lib/player/sampler/{sampler-warm,sampler-capture-vector}.ts',
		'tests/unit/color-fx-noise-entry.mjs',
		'tests/unit/variant-setting-entry.mjs',
		'tests/unit/variant-setting-test-row.mjs'
	],
	// Both Storybooks (Svelte in .storybook/, React twins in .storybook-react/):
	// knip's storybook plugin reads each main.ts for stories, addons and the
	// framework, so story files, the sandbox they mock and the addon packages
	// count as used. Named explicitly because the plugin only looks in
	// .storybook/ by default and would miss the React config.
	storybook: {
		config: ['.storybook/main.ts', '.storybook-react/main.ts'],
		entry: ['.storybook/preview.ts', '.storybook-react/preview.ts', 'src/**/*.stories.{ts,tsx}']
	},
	project: ['src/**/*.{js,ts,tsx,svelte}', 'tests/**/*.{js,ts,mjs}', 'scripts/**/*.{js,ts,mjs}'],
	ignoreBinaries: ['uv'],
	ignoreExportsUsedInFile: true,
	compilers: { svelte: svelteImports }
};
