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
const IMPORT_SPECIFIER =
	/(?<![.\w$])import(?![\w$.])(?:\s*\(\s*['"][^'"]+['"][^)]*\)|(?!\s*\()[^'"]+['"][^'"]+['"])/g;

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
		// Live cue alignment probes are run directly (node tests/live/<probe>.mjs
		// --url ...) against real audio hardware, so each one is an entry point,
		// not a module something is expected to import.
		'tests/live/cue-{align,bridge}-*.mjs',
		// Same shape: the RESCUE-05 cross-origin sink probe (real outputs, real origins).
		'tests/live/rescue-output-device-origin.mjs',
		// Same shape: the Beat Sync phase-lock workload, run directly against a live
		// engine's real beatgrids (read-only).
		'tests/live/phase-lock-jitter-workload.mjs',
		'tests/unit/fixtures/**/*.ts',
		'tests/e2e/fixtures/**/*.ts',
		'tests/manual/wkwebview-spike/inject.mjs',
		'tests/e2e/**/*.spec.ts',
		'tests/e2e/**/*.config.ts',
		'playwright.config.ts',
		'vite.config.ts',
		'svelte.config.js',
		'webui-port-config.ts',
		'scripts/**/*.{js,ts,mjs}',
		// Keyframe catalogue for ControlExplainer demos; consumed by unit tests, not runtime UI.
		'src/lib/rb/explainer-demo-keyframes.ts'
	],
	project: ['src/**/*.{js,ts,svelte}', 'tests/**/*.{js,ts,mjs}', 'scripts/**/*.{js,ts,mjs}'],
	ignoreBinaries: ['uv'],
	ignoreExportsUsedInFile: true,
	compilers: { svelte: svelteImports }
};
