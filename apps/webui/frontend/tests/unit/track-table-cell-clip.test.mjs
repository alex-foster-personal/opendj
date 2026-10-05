/**
 * LIBUX-35: every library table cell clips to its own column.
 *
 * SOURCE-SHAPE ON PURPOSE (see rating-cell-fit.test.mjs): the fact under test
 * is a CSS contract, and there is no component mount infra here.
 *
 * the maintainer, Thu 1 Oct 2026, on the demon-llama previews: "Artist col is spilling
 * over into other cols - no table cols should do this." The cause was one
 * per-column override, `.c-artist { overflow: visible; }` (5d799a5a5c), which
 * undid the shared `td` clip for that column alone, so a long artist list
 * painted over Key and BPM. The fix is the shared rule, not the column: the
 * generic `td` clips with an ellipsis, and a cell class may only stop
 * clipping when it names an inner element that clips its text instead.
 *
 * Regression lines:
 * - if the shared td rule stops clipping then every column can overflow
 * - if any .c-* cell rule sets overflow: visible without an inner clip then
 *   that column paints over its neighbour
 * - if a free-text cell drops its title then a clipped value cannot be read
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);
const browserDir = new URL('../../src/lib/components/rb/browser/', import.meta.url);
/** Child components that render a table cell of their own. */
const CELL_COMPONENTS = readdirSync(fileURLToPath(browserDir))
	.filter((f) => f.endsWith('.svelte') && f !== 'TrackTable.svelte')
	.map((f) => ({ f, src: readFileSync(fileURLToPath(new URL(f, browserDir)), 'utf8') }))
	.filter(({ src }) => /<td\b/.test(src));

/** Every `selector { body }` rule in a component's stylesheet, comments stripped. */
function rules(src = table) {
	const style = src.slice(src.lastIndexOf('<style>'));
	const css = style.replace(/\/\*[\s\S]*?\*\//g, '');
	const out = [];
	const re = /([^{}]+)\{([^{}]*)\}/g;
	for (let m = re.exec(css); m !== null; m = re.exec(css)) {
		out.push({ selector: m[1].trim(), body: m[2] });
	}
	return out;
}

/**
 * Cells allowed to stop clipping, each with the inner element that clips
 * its content instead. Adding a column here needs that inner clip.
 */
const INNER_CLIP = {
	// The deck buttons escape upwards (pin fce26c7493b0); .title-text clips.
	'.c-title': '.c-title .title-text',
	// Artwork is a fixed-size image filling the cell exactly; nothing to clip.
	'.c-art': null
};

/** The shared cell rule: unscoped td under the table's own rows. */
const SHARED_TD = ':where(tbody > tr) > :global(td)';

test('the shared td rule clips with an ellipsis', () => {
	const td = rules().find((r) => r.selector === SHARED_TD);
	assert.ok(td, `no shared ${SHARED_TD} rule in TrackTable`);
	assert.match(td.body, /overflow\s*:\s*hidden/);
	assert.match(td.body, /white-space\s*:\s*nowrap/);
	assert.match(td.body, /text-overflow\s*:\s*ellipsis/);
});

test('cells rendered by child components are reached by the shared rule', () => {
	assert.ok(CELL_COMPONENTS.length > 0, 'expected LyricColumn to render its own td');
	// A scoped `td` selector cannot match another component's element.
	assert.match(SHARED_TD, /:global\(td\)/);
	for (const { f, src } of CELL_COMPONENTS) {
		const offenders = rules(src).filter((r) => /overflow\s*:\s*visible/.test(r.body));
		assert.deepEqual(offenders.map((r) => r.selector), [], `${f} cell stops clipping`);
	}
});

test('no cell column overrides the clip unless an inner element clips instead', () => {
	const offenders = rules()
		.filter((r) => /overflow\s*:\s*visible/.test(r.body))
		.flatMap((r) => r.selector.split(',').map((s) => s.trim()))
		.filter((s) => /^\.c-[a-z-]+$/.test(s))
		.filter((s) => !(s in INNER_CLIP));
	assert.deepEqual(offenders, [], `cells that paint over their neighbour: ${offenders.join(', ')}`);
});

test('each allowed exception really clips its content', () => {
	for (const inner of Object.values(INNER_CLIP)) {
		if (inner === null) continue;
		const rule = rules().find((r) => r.selector === inner);
		assert.ok(rule, `no ${inner} rule`);
		assert.match(rule.body, /overflow\s*:\s*hidden/, `${inner} does not clip`);
		assert.match(rule.body, /text-overflow\s*:\s*ellipsis/, `${inner} has no ellipsis`);
	}
});

test('free-text cells carry their full value as a hover title', () => {
	for (const cls of ['c-title', 'c-artist', 'c-comments', 'c-genre']) {
		const at = table.indexOf(`class="${cls}"`);
		assert.notEqual(at, -1, `no ${cls} cell`);
		const tag = table.slice(at, table.indexOf('>', table.indexOf('title=', at)));
		assert.match(tag, /\btitle=\{/, `${cls} has no title tooltip`);
	}
});
