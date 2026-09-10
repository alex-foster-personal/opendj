/**
 * Load the production prefs module against Node's REAL Web Storage.
 *
 * Runs as a child process because real `localStorage` needs process flags
 * (`--experimental-webstorage --localstorage-file=...`) that the unit runner
 * does not set for every test. The blob to seed arrives on argv; the loaded
 * prefs go out as JSON on stdout.
 *
 * `window` is still a minimal host object, because Node has no window and
 * `_storage()` reads `window.localStorage`. What is no longer a stand-in is
 * the STORAGE: this is the WHATWG Storage implementation, with its real
 * string coercion, its real key semantics and its real persistence file, not
 * a Map wrapper that agrees with whatever the test expects.
 *
 * The module under test arrives pre-bundled (`bundlePath`, written once by
 * the caller's `before()`), not as a source path to compile here: three
 * captures used to mean three identical esbuild recompiles of the same
 * file, one per process, which is pure waste since none of them vary by
 * capture - only the seeded storage does.
 */
const [, , storageKey, blobJson, bundlePath] = process.argv;

localStorage.setItem(storageKey, blobJson);
globalThis.window = { localStorage };

// $state is an identity function under test - see load-typescript.mjs's
// loadTypeScriptModule, which this mirrors for the pre-bundled path.
globalThis.__musicDjToolsTestState = (value) => value;
globalThis.__musicDjToolsTestState.snapshot = (value) =>
	value === undefined ? undefined : JSON.parse(JSON.stringify(value));

const prefs = await import(bundlePath);
process.stdout.write(JSON.stringify({ ...prefs.uiPrefs, __keys: Object.keys(prefs.uiPrefs) }));
