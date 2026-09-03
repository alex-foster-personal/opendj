/**
 * Source-shape regression pinning SuggestNextStrip.svelte's transport
 * conversion onto the generated OpenAPI client.
 *
 * This is deliberately NOT a behavioral test: the repo has no component
 * mount infrastructure (no jsdom/happy-dom, and the esbuild test loader is
 * TypeScript-only, so a .svelte file cannot be imported here). The repo
 * idiom for .svelte assertions is source-level (see inert-controls.test.mjs).
 * What this pins:
 * - no raw fetch() call remains in the component
 * - the call goes through api.POST with the exact schema path literal
 * - the client import replaced the RB_API_BASE import
 * - the 422 insufficient-data outcome is mapped off ApiError
 * - the body carries explain: false (required by the generated SuggestNextIn;
 *   false is the server default the old raw fetch relied on)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const COMPONENT = fileURLToPath(
	new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)
);
const source = readFileSync(COMPONENT, 'utf8');

test('no raw fetch call remains in the component', () => {
	assert.doesNotMatch(source, /(^|[^A-Za-z0-9_])fetch\(/);
});

test('the suggest-next call goes through the generated client with the schema path', () => {
	assert.match(source, /api\.POST\('\/api\/v1\/copilot\/suggest-next'/);
	assert.match(source, /import \{ ApiError, api, unwrap \} from '\$lib\/api\/client';/);
	assert.doesNotMatch(source, /RB_API_BASE/);
});

test('the 422 insufficient-data outcome is mapped off ApiError', () => {
	assert.match(source, /e instanceof ApiError && e\.status === 422/);
});

test('the request body sends the explicit explain: false server default', () => {
	assert.match(source, /explain: false/);
});

// LIBUX-03: "Next does not need to be so tall. remove bpm match and camelot
// step." The wire types and oncandidates plumbing still carry bpm/key_camelot
// (RecommendedSection depends on that data) -- only the tile's OWN template
// markup drops the readouts. Scope every check to the template, i.e.
// everything after the last </script>, so the still-legitimate script-side
// references (interfaces, _toCandidates) do not false-positive the test.
const template = source.slice(source.lastIndexOf('</script>'));

test('the candidate tile renders no bpm readout', () => {
	assert.doesNotMatch(template, /cand\.bpm/);
});

test('the candidate tile renders no camelot step readout', () => {
	assert.doesNotMatch(template, /cand\.key_camelot/);
});

test('the meta row is removed entirely, not just trimmed, so the tile height drops', () => {
	// A trimmed-but-present meta row would still cost the tile a whole
	// flex row of height. LIBUX-03's acceptance test is explicit that
	// hiding the readouts without the height dropping is a fail, so the
	// row itself -- not just the bpm/camelot text inside it -- must go.
	assert.doesNotMatch(template, /class="meta"/);
});

