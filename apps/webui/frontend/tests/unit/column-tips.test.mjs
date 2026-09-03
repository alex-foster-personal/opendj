import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// M22 - every library column header carries an explanatory hover title, and all
// of them resolve through the one module. This was a LIVE regression: the module
// shipped with all 12 tips and a helper, nothing imported it, and the Preview and
// Artwork headers carried no `title` at all. The copy survived; the wiring did not.
//
// Regression lines:
// - if TrackTable stops importing column-tips then the module is orphaned again
// - if any LibraryColTipId is not wired to a header then a titleless header ships
// - if the Preview or Artwork header loses its title then the two headers that
//   regressed last time are back to silent
// - if columnHeaderTitle drops the sort hint then sortable headers stop saying
//   which way a click sorts
// - if a header inlines a copy of a COLUMN_TIPS string then a second copy exists
//   to diverge from the module

const TRACK_TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);

let tips;
let source;

before(async () => {
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
	// Line endings are a checkout detail, not source content: a Windows
	// checkout carries CRLF, and the '>\n' tag scan below would find none.
	source = readFileSync(TRACK_TABLE, 'utf8').replaceAll('\r\n', '\n');
});

/** Every `title={...}` expression in the file, brace-aware. */
function titleExpressions(text) {
	const out = [];
	const marker = 'title={';
	for (let i = text.indexOf(marker); i >= 0; i = text.indexOf(marker, i + 1)) {
		let depth = 1;
		let j = i + marker.length;
		for (; j < text.length && depth > 0; j++) {
			if (text[j] === '{') depth++;
			else if (text[j] === '}') depth--;
		}
		out.push(text.slice(i + marker.length, j - 1));
	}
	return out;
}

// ------------------------------------------------------------- the module

test('every column tip id has real explanatory copy behind it', () => {
	const ids = Object.keys(tips.COLUMN_TIPS);
	assert.equal(ids.length, 12, `expected 12 column tips, got ${ids.length}: ${ids.join(', ')}`);
	for (const [id, tip] of Object.entries(tips.COLUMN_TIPS)) {
		assert.equal(typeof tip, 'string', `${id} has no tip string`);
		assert.ok(
			tip.trim().length >= 20,
			`${id}'s tip is ${tip.trim().length} chars - too short to explain the column`
		);
	}
});

test('columnHeaderTitle returns the bare tip when there is no sort hint', () => {
	for (const [id, tip] of Object.entries(tips.COLUMN_TIPS)) {
		assert.equal(tips.columnHeaderTitle(id), tip, `${id} without a hint must be the bare tip`);
		assert.equal(tips.columnHeaderTitle(id, null), tip);
		assert.equal(tips.columnHeaderTitle(id, ''), tip);
	}
});

test('columnHeaderTitle keeps BOTH the explainer and the sort hint', () => {
	const hint = 'Sort by Artist (asc -> desc -> clear)';
	const title = tips.columnHeaderTitle('artist', hint);

	assert.ok(
		title.includes(tips.COLUMN_TIPS.artist),
		'the explainer was dropped, so the header only says how to sort, not what the column is'
	);
	assert.ok(title.includes(hint), 'the sort hint was dropped');
	assert.ok(
		title.indexOf(tips.COLUMN_TIPS.artist) < title.indexOf(hint),
		'the explainer must lead; the sort mechanics are the suffix'
	);
});

// ------------------------------------------------- the wiring into the table

test('TrackTable resolves header titles through the column-tips module', () => {
	assert.match(
		source,
		/import \{[^}]*columnHeaderTitle[^}]*\} from '\$lib\/rb\/column-tips'/,
		'TrackTable no longer imports column-tips - the module is orphaned again'
	);
});

test('every column tip id is actually wired to a header', () => {
	for (const id of Object.keys(tips.COLUMN_TIPS)) {
		const wired =
			source.includes(`columnHeaderTitle('${id}'`) ||
			// the sortable headers pass their ColId through the shared snippet
			(source.includes('columnHeaderTitle(col,') && source.includes(`, '${id}')}`));
		assert.ok(wired, `column '${id}' has a tip nobody renders - a titleless header ships`);
	}
});

test('the Preview and Artwork headers carry a title (the pair that regressed)', () => {
	for (const [cls, id] of [
		['h-preview', 'preview'],
		['h-art', 'art']
	]) {
		const at = source.indexOf(`class="${cls}"`);
		assert.ok(at > 0, `no <th class="${cls}"> found - the header moved, find it`);
		// The opening tag runs from the `<th` before the class to the next `>`
		// at brace depth zero; the title must live inside it.
		const open = source.lastIndexOf('<th', at);
		const tag = source.slice(open, source.indexOf('>\n', at) + 1);
		assert.ok(
			tag.includes(`columnHeaderTitle('${id}')`),
			`the ${id} header carries no column-tips title:\n${tag}`
		);
	}
});

test('the sortable header snippet titles every column it renders', () => {
	const snippet = source.slice(
		source.indexOf('{#snippet sortableTh('),
		source.indexOf('{/snippet}', source.indexOf('{#snippet sortableTh('))
	);
	assert.ok(snippet.length > 0, 'the sortableTh snippet is gone - find where headers render now');
	assert.ok(
		snippet.includes('columnHeaderTitle(col,'),
		'sortableTh builds its own title string instead of resolving through column-tips'
	);
});

test('no header inlines its own copy of a tip string', () => {
	const expressions = titleExpressions(source);
	for (const [id, tip] of Object.entries(tips.COLUMN_TIPS)) {
		for (const expression of expressions) {
			if (expression.includes('columnHeaderTitle')) continue;
			assert.ok(
				!expression.includes(tip.slice(0, 40)),
				`a title inlines the '${id}' tip instead of importing it - the copies will diverge`
			);
		}
	}
});

test('TrackTable keeps compact K/B cells and explains exact BPM plus unavailable dynamic analysis', () => {
	assert.match(source, /key: 36/);
	assert.match(source, /bpm: 42/);
	assert.match(source, /camelot-suffix/);
	assert.match(source, /Exact BPM: .*toFixed\(1\)/);
	assert.match(source, /Dynamic key analysis: not analyzed/);
	assert.match(source, /Dynamic tempo analysis: not analyzed/);
});
