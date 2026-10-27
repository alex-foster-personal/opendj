import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Key notation is ONE display setting applied to every row (operator
 * report, Sat 24 Oct 2026). the reporting operator's library mixed rekordbox's Camelot "8A" with its spelled "Am",
 * "F#m" and "Fmaj" in the same table, because each cell printed the stored
 * string.
 *
 * [if] two notations of one key render differently under one setting [then]
 *   the table can still mix notations - broken.
 * [if] a key in no notation renders as anything but its raw text, unflagged
 *   [then] the cell is guessing - broken.
 * [if] the key cell prints row.key directly [then] the setting is bypassed - broken.
 */

let notation;

before(async () => {
	notation = await loadTypeScriptModule('src/lib/rb/key-notation.ts');
});

/** Every spelling seen in a real operator library (Sat 24 Oct 2026,
 * track_fields.key and the served listing), plus the Open Key, Unicode
 * accidental and spelled-mode variants other sources write. Expected values
 * are hand-checked against the published Camelot wheel. */
const SPELLINGS = [
	// rekordbox spelled minors, all 12 observed
	['Gm', '6A', '11m', 'Gm'],
	['Dm', '7A', '12m', 'Dm'],
	['Fm', '4A', '9m', 'Fm'],
	['F#m', '11A', '4m', 'F#m'],
	['Em', '9A', '2m', 'Em'],
	['Am', '8A', '1m', 'Am'],
	['Bm', '10A', '3m', 'Bm'],
	['Cm', '5A', '10m', 'Cm'],
	['Abm', '1A', '6m', 'Abm'],
	['Dbm', '12A', '5m', 'Dbm'],
	['Ebm', '2A', '7m', 'Ebm'],
	['Bbm', '3A', '8m', 'Bbm'],
	// rekordbox spelled majors, all observed
	['C', '8B', '1d', 'C'],
	['D', '10B', '3d', 'D'],
	['E', '12B', '5d', 'E'],
	['A', '11B', '4d', 'A'],
	['Ab', '4B', '9d', 'Ab'],
	['F', '7B', '12d', 'F'],
	['Eb', '5B', '10d', 'Eb'],
	['Db', '3B', '8d', 'Db'],
	['F#', '2B', '7d', 'F#'],
	['G', '9B', '2d', 'G'],
	['B', '1B', '6d', 'B'],
	['Bb', '6B', '11d', 'Bb'],
	['Fmaj', '7B', '12d', 'F'],
	// Camelot as stored by rekordbox and MIK
	['8A', '8A', '1m', 'Am'],
	['12B', '12B', '5d', 'E'],
	['1a', '1A', '6m', 'Abm'],
	// Open Key (Traktor / MIK)
	['1m', '8A', '1m', 'Am'],
	['12d', '7B', '12d', 'F'],
	['6m', '1A', '6m', 'Abm'],
	// sharps vs flats, Unicode accidentals, spelled modes
	['G#m', '1A', '6m', 'Abm'],
	['C#m', '12A', '5m', 'Dbm'],
	['Gb', '2B', '7d', 'F#'],
	['E♭m', '2A', '7m', 'Ebm'],
	['F♯', '2B', '7d', 'F#'],
	['A minor', '8A', '1m', 'Am'],
	['A min', '8A', '1m', 'Am'],
	['Bbmaj', '6B', '11d', 'Bb'],
	['C# major', '3B', '8d', 'Db'],
	[' 8A ', '8A', '1m', 'Am']
];

describe('displayKey renders every stored spelling in the ONE chosen notation', () => {
	for (const [stored, camelot, openKey, musical] of SPELLINGS) {
		it(`${JSON.stringify(stored)} -> ${camelot} / ${openKey} / ${musical}`, () => {
			assert.deepEqual(notation.displayKey(stored, 'camelot'), { text: camelot, parsed: true });
			assert.deepEqual(notation.displayKey(stored, 'open_key'), { text: openKey, parsed: true });
			assert.deepEqual(notation.displayKey(stored, 'musical'), { text: musical, parsed: true });
			assert.deepEqual(notation.displayKey(stored, 'camelot_musical'), {
				text: `${camelot} \u2022 ${musical}`,
				parsed: true
			});
		});
	}
});

it('one setting never mixes notations: "8A" and "Am" render identically', () => {
	for (const n of notation.KEY_NOTATIONS) {
		assert.equal(notation.displayKey('8A', n).text, notation.displayKey('Am', n).text, n);
	}
});

it('camelot_musical is the default and reads "8A • Am" (lowercase m = minor, major is the bare root)', () => {
	assert.equal(notation.DEFAULT_KEY_NOTATION, 'camelot_musical');
	assert.equal(notation.KEY_NOTATIONS[0], 'camelot_musical');
	assert.equal(notation.formatKey('8A', 'camelot_musical'), '8A \u2022 Am');
	assert.equal(notation.formatKey('8B', 'camelot_musical'), '8B \u2022 C');
	assert.equal(notation.formatKey('C#m', 'camelot_musical'), '12A \u2022 Dbm');
	assert.equal(notation.formatKey(null, 'camelot_musical'), null);
	assert.equal(notation.formatKey('All', 'camelot_musical'), 'All');
});

it('a camelot_musical string whose halves disagree is not a key', () => {
	assert.deepEqual(notation.displayKey('8A \u2022 C', 'camelot'), { text: '8A \u2022 C', parsed: false });
});

it('allKeyTexts lists all 24 keys in a notation', () => {
	for (const n of notation.KEY_NOTATIONS) assert.equal(new Set(notation.allKeyTexts(n)).size, 24, n);
});

it('a key in no notation is shown as stored and flagged, never guessed', () => {
	for (const raw of ['All', 'H', '13A', '0m', 'Amaj7', '8M']) {
		assert.deepEqual(notation.displayKey(raw, 'musical'), { text: raw, parsed: false }, raw);
	}
});

it('no key at all is null, not an empty string cell', () => {
	assert.equal(notation.displayKey(null, 'camelot'), null);
	assert.equal(notation.displayKey('', 'camelot'), null);
	assert.equal(notation.displayKey('  ', 'camelot'), null);
});

it('every Camelot key round-trips through all three notations', () => {
	for (let n = 1; n <= 12; n += 1) {
		for (const mode of ['A', 'B']) {
			const camelot = `${n}${mode}`;
			for (const target of notation.KEY_NOTATIONS) {
				const rendered = notation.displayKey(camelot, target).text;
				assert.equal(notation.displayKey(rendered, 'camelot').text, camelot, `${camelot} via ${target}`);
			}
		}
	}
});

it('the key cell renders through the notation setting, not row.key', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const start = table.indexOf('class="c-key"');
	assert.notEqual(start, -1, 'no Key cell in TrackTable');
	const cell = table.slice(table.lastIndexOf('<td', start), table.indexOf('</td>', start));
	assert.doesNotMatch(cell, /hl\(row\.key\)/, 'key cell prints the stored key string');
	assert.match(table, /displayKey\(row\.key, uiPrefs\.key_notation\)/, 'key cell ignores the key_notation setting');
	assert.match(cell, /class:key-unparsed=/, 'an unparsed key is not flagged');
});

it('keyForms lists the key in every notation, so key: search ignores the setting', () => {
	assert.deepEqual(notation.keyForms('Am'), ['8A \u2022 Am', '8A', '1m', 'Am']);
	assert.deepEqual(notation.keyForms('8A'), ['8A \u2022 Am', '8A', '1m', 'Am']);
	assert.deepEqual(notation.keyForms('H#'), ['H#']);
	assert.deepEqual(notation.keyForms(null), []);
});

describe('GET /ui-prefs hydrates key_notation', () => {
	let prefs;
	let originalFetch;
	const jsonResponse = (body) =>
		new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

	before(async () => {
		prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
			viteApiBase: 'https://key-notation-hydrate.example.test'
		});
		originalFetch = globalThis.fetch;
	});
	after(() => {
		globalThis.fetch = originalFetch;
	});

	it('a valid key_notation from disk is applied', async () => {
		prefs.uiPrefs.key_notation = 'camelot';
		globalThis.fetch = async () => jsonResponse({ key_notation: 'musical' });
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.equal(prefs.uiPrefs.key_notation, 'musical');
	});

	for (const [label, body] of [
		['an invalid', { key_notation: 'traktor', library_watcher_folders: ['/x'] }],
		['a missing', { library_watcher_folders: ['/x'] }]
	]) {
		it(`${label} key_notation fails hydration and the rejection reaches the caller`, async () => {
			prefs.uiPrefs.key_notation = 'open_key';
			prefs.uiPrefs.library_watcher_folders = [];
			globalThis.fetch = async () => jsonResponse(body);
			await assert.rejects(prefs.hydrateConfirmPrefsFromDisk(), /key_notation must be camelot_musical\|camelot\|open_key\|musical/);
			assert.equal(prefs.uiPrefs.key_notation, 'open_key', 'a contract-violating value was applied');
			assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/x'], 'the rest of the response still hydrates');
		});
	}

	it('a transport failure rejects too, never a silent stale notation (Sol P1, PR #7483)', async () => {
		prefs.uiPrefs.key_notation = 'musical';
		globalThis.fetch = async () => {
			throw new TypeError('fetch failed');
		};
		await assert.rejects(prefs.hydrateConfirmPrefsFromDisk(), /fetch failed/);
		assert.equal(prefs.uiPrefs.key_notation, 'musical');
	});

	it('the boot path shows a hydration failure instead of swallowing it', () => {
		const panel = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
			'utf8'
		);
		assert.doesNotMatch(panel, /prefsPromise\.catch\(\(\) => \{\}\)/, 'boot swallows a prefs failure');
		assert.match(panel, /prefsPromise\.catch\(\(exc: unknown\) => \{\s*pushToast\([^\n]*\);\s*throw exc;\s*\}\)/, 'a prefs failure is toasted but the boot carries on');
	});
});

describe('find highlighting in the key cell follows the match in any notation (Sol P2, PR #7483)', () => {
	let find;
	before(async () => {
		find = await loadTypeScriptModule('src/lib/rb/find-highlight.ts');
	});
	const row = (key) => ({ title: null, artist: null, comments: null, key, genre: null });

	it('finding "11A" while the cell reads "F#m" matches the row AND marks the shown key', () => {
		assert.equal(find.rowMatchesFind(row('11A'), 'F#m'), true);
		assert.equal(find.rowMatchesFind(row('11A'), '11A'), true);
		assert.deepEqual(find.keyCellSpans('11A', 'musical', '11A'), [{ text: 'F#m', hit: true }]);
		assert.deepEqual(find.keyCellSpans('11A', 'open_key', 'F#m'), [{ text: '4m', hit: true }]);
	});

	it('a direct match keeps its exact span, and a non-match marks nothing', () => {
		assert.deepEqual(find.keyCellSpans('11A', 'musical', 'F#m'), [{ text: 'F#m', hit: true }]);
		assert.deepEqual(find.keyCellSpans('11A', 'musical', '12A'), [{ text: 'F#m', hit: false }]);
		assert.equal(find.rowMatchesFind(row('11A'), '12A'), false);
	});
});

describe('every key display outside the table honours key_notation', () => {
	const SITES = {
		'src/lib/components/rb/deck/DeckHeader.svelte': [/>\{keyShown\}<\/span/, /shownKey\(keyText\)/, />\{origKeyShown\}<\/span>/],
		'src/lib/components/library/TrackInspector.svelte': [/shownKey\(single\.key\)/],
		'src/lib/components/library/LibraryPreviewBar.svelte': [/shownKey\(row\.key\)/],
		'src/lib/components/rb/CommandBar.svelte': [/shownKey\(row\.key\)/],
		'src/lib/components/rb/browser/AutoPlayWalkthrough.svelte': [/shownKey\(row\.key\)/]
	};
	for (const [rel, patterns] of Object.entries(SITES)) {
		it(rel, () => {
			// shownKey (key-notation.ts) reads the source prefs.svelte.ts installs.
			const src = readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
			for (const pattern of patterns) assert.match(src, pattern, `${rel} prints a key outside the setting`);
		});
	}
	it('the deck header no longer prints the raw stored or playhead key', () => {
		const src = readFileSync(
			fileURLToPath(new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)),
			'utf8'
		);
		assert.doesNotMatch(src, />\{keyText\}</);
		assert.doesNotMatch(src, />\{deck\.key\}</);
	});
});

it('shownKey reads the notation source prefs.svelte.ts installs, and refuses before it', async () => {
	const fresh = await loadTypeScriptModule('src/lib/rb/key-notation.ts');
	assert.throws(() => fresh.shownKey('8A'), /no key notation source/);
	let current = 'camelot_musical';
	fresh.installKeyNotationSource(() => current);
	assert.equal(fresh.shownKey('Am'), '8A \u2022 Am');
	current = 'open_key';
	assert.equal(fresh.shownKey('Am'), '1m');
	assert.equal(fresh.shownKey(null), null);
	const prefsSrc = readFileSync(fileURLToPath(new URL('../../src/lib/rb/prefs.svelte.ts', import.meta.url)), 'utf8');
	assert.match(prefsSrc, /installKeyNotationSource\(\(\) => uiPrefs\.key_notation\)/);
});
