/**
 * requirement: UX-FLOAT-01
 *
 * The performance quick-draw menu only exists after a right-click, so it is
 * fetched by that right-click instead of riding in the /performance boot
 * bundle (PR #4011 put `performance` over its budget; deferring the menu paid
 * it back). These checks read the source because the property under test is
 * the import SHAPE: a static import anywhere on the boot path would silently
 * put the bytes back, and the bundle gate only notices once it is over.
 * Same pattern and same checks as PR #3896's MIDI drawer and row popovers.
 *
 * Regression lines:
 *   if the /performance page statically imports QuickDrawMenu.svelte then broken
 *   if QuickDrawMenuLoader imports QuickDrawMenu.svelte other than by import() then broken
 *   if a failed QuickDrawMenu import renders no role="alert" then broken
 *   if the loader does not hand the triggering right-click to the menu then broken
 *   if the menu ignores the handed-over right-click on mount then broken
 *   if the lazy-load error promises a same-document retry, lacks Reload, or
 *     reloads without a click then broken
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => fs.readFileSync(path.join(FRONTEND_ROOT, rel), 'utf8');

const performancePage = read('src/routes/performance/+page.svelte');
const quickDrawLoader = read('src/lib/components/rb/QuickDrawMenuLoader.svelte');
const quickDrawMenu = read('src/lib/components/rb/QuickDrawMenu.svelte');

// A static import is `import X from '...Y.svelte'`; import('...') is dynamic.
const staticImportOf = (file) => new RegExp(`import\\s+\\w+\\s+from\\s+['"][^'"]*${file}['"]`);
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*${file}['"]\\s*\\)`);

test('/performance mounts the quick-draw menu through the lazy loader, never statically', () => {
	assert.doesNotMatch(performancePage, staticImportOf('QuickDrawMenu\\.svelte'));
	assert.match(performancePage, staticImportOf('QuickDrawMenuLoader\\.svelte'));
	assert.match(performancePage, /<QuickDrawMenuLoader \/>/);
});

test('the quick-draw module is fetched by a .perf-root right-click, and a failure is shown inline', () => {
	assert.doesNotMatch(quickDrawLoader, staticImportOf('QuickDrawMenu\\.svelte'));
	assert.match(quickDrawLoader, dynamicImportOf('QuickDrawMenu\\.svelte'));
	assert.match(quickDrawLoader, /<svelte:window oncontextmenu=\{onContextMenu\} \/>/);
	assert.match(quickDrawLoader, /closest\('\.perf-root'\)/);
	assert.match(quickDrawLoader, /\.catch\(/);
	assert.equal((quickDrawLoader.match(/role="alert"/g) ?? []).length, 1);
	assert.match(quickDrawLoader, /Quick-draw menu failed to load: \{loadError\}/);
	assert.doesNotMatch(quickDrawLoader, /pushToast/, 'the failure is inline, never a toast');
});

test('the right-click that fetched the menu is the one it opens for', () => {
	assert.match(quickDrawLoader, /<QuickDrawMenuComponent initialEvent=\{pendingEvent\} \/>/);
	assert.match(quickDrawMenu, /let \{ initialEvent = null \}: \{ initialEvent\?: MouseEvent \| null \} = \$props\(\);/);
	assert.match(quickDrawMenu, /if \(initialEvent !== null\) onContextMenu\(initialEvent\);/);
});

// A failed import() stays failed in its document (the browser's module map
// keeps it), so the only recovery is a fresh document the user asks for.
test('the lazy-load error offers Reload on click, and never promises a same-document retry', () => {
	// The markup is what the user reads; comments may describe the old promise.
	const markup = quickDrawLoader.slice(quickDrawLoader.lastIndexOf('</script>'));
	assert.match(markup, /failed to load/);
	assert.doesNotMatch(markup, /reopen|retry/i);
	const reloads = quickDrawLoader.match(/location\.reload\(\)/g) ?? [];
	const onClick = quickDrawLoader.match(/onclick=\{\(\) => location\.reload\(\)\}>Reload</g) ?? [];
	assert.equal(onClick.length, 1, 'one Reload button on the error surface');
	assert.equal(reloads.length, onClick.length, 'location.reload() only inside a Reload click');
});
