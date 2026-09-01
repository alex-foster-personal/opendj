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
 */
import { loadTypeScriptModule } from './load-typescript.mjs';

const [, , storageKey, blobJson] = process.argv;

localStorage.setItem(storageKey, blobJson);
globalThis.window = { localStorage };

const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
process.stdout.write(JSON.stringify({ ...prefs.uiPrefs, __keys: Object.keys(prefs.uiPrefs) }));
