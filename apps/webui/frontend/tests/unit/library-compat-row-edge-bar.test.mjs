// requirement: LIBM-186
// [if] a library row is key+BPM-compatible with the master [then] it shows a thin bar on its left edge only (3px wide, 75% of the row height, vertically centred) and no row wash at rest or on hover in any skin, [else stop].
// [if] the row is selected, menu-highlighted, loaded, master, deck-hovered, a job row or Spotify-pending [then] the bar is not drawn over that state's own edge or fill, [else stop].
// [if] the skin is default, Gothic or light [then] the bar color is a muted mix of the skin's green and dim text, [else stop].
// [if] the match is compatible [then] the key cell keeps its ring and the BPM cell keeps its green/red verdict unchanged, [else stop].
// requirement: LIBM-8699
// [if] a compatible row draws its bar [then] the bar rides the row's first cell (td.c-funnel::before), never the <tr>'s own ::before, [else stop: Chrome/WebKit lay a row ::before out as a leading cell and every column of that row shifts one slot right].
// [if] any new TrackTable rule puts ::before on a body row [then] this guard fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');
const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
const STYLE = TABLE.slice(TABLE.indexOf('<style'));

/** Every `{...}` body whose selector contains `needle`, with its selector. */
function rules(needle) {
	return [...STYLE.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
		.filter((m) => m[1].includes(needle))
		.map((m) => ({ selector: m[1].replace(/\s+/g, ' ').replace(/\( /g, '(').replace(/ \)/g, ')').trim(), body: m[2] }));
}

const bar = rules('tr.rb-row-mix-compatible');

test('a compatible row gets a left-edge bar, not a row wash', () => {
	assert.equal(bar.length, 1, `expected one compatible-row rule, got ${bar.map((r) => r.selector).join(' | ')}`);
	const { selector, body } = bar[0];
	assert.match(selector, / > td\.c-funnel::before$/);
	assert.doesNotMatch(body, /(^|[^-])background:\s*color-mix\(in srgb, var\(--rb-green\) (9|15)%/);
	assert.match(body, /position:\s*absolute;/);
	assert.match(body, /left:\s*0;/);
	assert.match(body, /width:\s*3px;/);
	assert.match(body, /height:\s*75%;/);
	assert.match(body, /top:\s*12\.5%;/);
	assert.match(body, /pointer-events:\s*none;/);
});

test('the bar stays off every row state that owns the left edge or row fill', () => {
	for (const cls of ['rb-row-selected', 'rb-row-menu', 'loaded', 'rb-row-master', 'rb-row-spotify-pending', 'rb-row-deck-hover', 'rb-row-job']) {
		assert.ok(bar[0].selector.includes(`:not(.${cls})`), `bar rule must exclude .${cls}`);
	}
});

test('the bar uses a muted theme token in every skin', () => {
	assert.match(bar[0].body, /background:\s*var\(--rb-mix-compat-bar, color-mix\(in srgb, var\(--rb-green\) \d+%, var\(--rb-text-dim\)\)\);/);
	assert.match(THEME, /--rb-mix-compat-bar:\s*initial;/);
	assert.doesNotMatch(THEME, /--rb-mix-compat-row-(tint|hover)/, 'the old wash tokens are gone');
});

test('control: key ring and BPM verdict styling are unchanged', () => {
	assert.match(STYLE, /\.c-key\.key-compat::after[\s\S]*?border: 1px solid var\(--rb-key-compat-border, var\(--key-compat-color\)\)/);
	assert.match(STYLE, /\.c-bpm\.bpm-compatible \{ box-shadow: inset 0 0 0 1px var\(--rb-green\); \}/);
	assert.match(STYLE, /\.c-bpm\.bpm-danger \{ box-shadow: inset 0 0 0 1px var\(--rb-bpm-mismatch-ring, #e14238\); \}/);
});

/** The last compound of every comma part of every rule, e.g. `tr.x:not(.y)::before`. */
function lastCompounds() {
	return [...STYLE.matchAll(/([^{}]+)\{[^{}]*\}/g)].flatMap((m) =>
		m[1]
			.replace(/\/\*[\s\S]*?\*\//g, '')
			.replace(/\(\s+/g, '(')
			.replace(/\s+\)/g, ')')
			.split(',')
			.map((part) => part.trim().split(/\s+|>/).filter(Boolean).at(-1) ?? '')
	);
}

test('LIBM-8699: no body-row rule hangs a ::before on the <tr> itself (it becomes a leading cell)', () => {
	// tbody tr.rb-row-job::before is the one older offender, kept by name so the
	// guard still bites any new rule; its job wash needs a row-wide fill that a
	// clipping first cell cannot draw (follow-up).
	const KNOWN = new Set(['tr.rb-row-job::before']);
	const offenders = lastCompounds().filter((c) => /^tr\b/.test(c) && /::?before$/.test(c) && !KNOWN.has(c));
	assert.deepEqual(offenders, [], `row ::before shifts every column of that row one slot right: ${offenders.join(' | ')}`);
});

test('LIBM-8699 control: the guard sees the row ::after separator and the job wash', () => {
	const all = lastCompounds();
	assert.ok(all.includes('tr::after'), 'row separator rule is parsed');
	assert.ok(all.includes('tr.rb-row-job::before'), 'job wash rule is parsed');
});
