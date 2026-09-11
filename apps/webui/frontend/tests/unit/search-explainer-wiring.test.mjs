/**
 * SearchBox.svelte's search-syntax explainer (pin 7ca47b21ead7, issue #936).
 *
 * SOURCE-SHAPE ON PURPOSE - same precedent as column-tips.test.mjs and
 * jobs-drawer.test.mjs: no component mount infra (no jsdom, no
 * @testing-library) in this harness, so a .svelte file cannot be rendered
 * and queried here. What CAN be executed (the grammar itself, and that its
 * documented examples actually parse) is covered in
 * browser-search-query.test.mjs; this file pins the markup-only fact that
 * the explainer is actually wired up and reused rather than reinvented.
 *
 * Regression lines:
 * - if SearchBox stops importing ControlExplainer then the search box has
 *   gone back to teaching nobody the field:operator:value grammar
 * - if SearchBox starts importing a bespoke/new explainer component instead
 *   of the existing ControlExplainer/AutoPlayExplainer pattern then a third
 *   explainer pattern has been invented against the pin's explicit ask
 * - if the explainer's bullets stop being sourced from
 *   browser-search-query.ts's SEARCH_QUERY_HELP then the taught syntax can
 *   drift from what the parser actually accepts
 * - if the search input's native title/popover overlap comes back (the
 *   exact class of bug ControlExplainer's own doc comment warns about)
 *   then hovering the search box shows two competing tooltips at once
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SEARCH_BOX = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/SearchBox.svelte', import.meta.url)
);

const source = readFileSync(SEARCH_BOX, 'utf8').replaceAll('\r\n', '\n');

test('SearchBox imports the existing ControlExplainer pattern (not a bespoke one)', () => {
	assert.match(source, /import ControlExplainer from ['"]\.\.\/deck\/ControlExplainer\.svelte['"]/);
	assert.match(source, /<ControlExplainer\b/);
});

test('the explainer bullets are sourced from browser-search-query.ts, not inlined copy', () => {
	assert.match(source, /import \{ SEARCH_QUERY_HELP \} from ['"]\$lib\/rb\/browser-search-query['"]/);
	assert.match(source, /SEARCH_QUERY_HELP\.map/);
});

test('the search input no longer carries a native title alongside the explainer (overlap regression)', () => {
	// The label used to carry title={modeTitle} directly; that is now only
	// reachable via aria-label, so ControlExplainer's own popover heading is
	// the single source of hover copy.
	assert.doesNotMatch(source, /class="rb-search"[^>]*\btitle=/);
	assert.match(source, /class="rb-search"[^>]*\baria-label=\{modeTitle\}/);
});
