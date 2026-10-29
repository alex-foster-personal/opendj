import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..');
const BROWSER_PANEL = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/BrowserPanel.svelte'),
	'utf8'
);
const KEY_FILTER_PANEL = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/browser/LibraryKeyFilterPanel.svelte'),
	'utf8'
);
const KEY_FILTER_POPOVER = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/browser/LibraryKeyFilterPopover.svelte'),
	'utf8'
);

test('wheel geometry: 24 segments, B outer before A; compatible = ref, +/-1, A/B swap', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	const segments = mod.libraryKeyWheelSegments();
	assert.equal(segments.length, 24);
	assert.equal(segments[0].code, '1B');
	assert.equal(segments[1].code, '1A');
	assert.equal(segments[22].code, '12B');
	assert.equal(segments[23].code, '12A');
	assert.equal(new Set(segments.map((s) => s.code)).size, 24);
	const eight = mod.compatibleLibraryKeys('8A');
	assert.deepEqual(eight, ['7A', '8B', '8A', '9A']);
	const wrap = mod.compatibleLibraryKeys('12A');
	assert.deepEqual(wrap, ['1A', '11A', '12B', '12A']);
	assert.deepEqual(mod.compatibleLibraryKeys('1B'), ['1B', '1A', '2B', '12B']);
	assert.throws(() => mod.compatibleLibraryKeys('Am'));
});

test('segment colour is the track table swatch (camelotKeyColor), skin variables included', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	const color = await loadTypeScriptModule('src/lib/rb/camelot-color.ts');
	for (let n = 1; n <= 12; n += 1) {
		for (const mode of ['A', 'B']) {
			assert.equal(mod.libraryKeySegmentFill(n, mode), color.camelotKeyColor(`${n}${mode}`));
		}
		assert.match(mod.libraryKeySegmentFill(n, 'B'), new RegExp(`^hsl\\(${color.camelotNumberHue(n)} `));
	}
	assert.match(mod.libraryKeySegmentFill(8, 'B'), /var\(--rb-key-lightness-major/);
	// A CSS fill style, not an SVG attribute, so the skin variables resolve.
	assert.match(KEY_FILTER_PANEL, /style:fill=\{libraryKeySegmentFill\(/);
	assert.doesNotMatch(
		readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/library-key-filter.ts'), 'utf8'),
		/HUES\s*[:=]/
	);
});

test('selected keys stay bright, the rest dim; nothing selected = all bright', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	assert.equal(mod.libraryKeySegmentOpacity(false, false), 1);
	assert.equal(mod.libraryKeySegmentOpacity(true, true), 1);
	assert.equal(mod.libraryKeySegmentOpacity(false, true), mod.LIBRARY_KEY_DIM_OPACITY);
	assert.ok(mod.LIBRARY_KEY_DIM_OPACITY < 0.5);
});

test('notation labels for all four key_notation modes', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	for (const notation of ['camelot', 'camelot_musical', 'open_key', 'musical']) {
		const lines = mod.libraryKeyWheelLabelLines('8A', notation);
		assert.ok(lines.line1.length > 0, notation);
	}
	const lines = mod.libraryKeyWheelLabelLines('8A', 'camelot_musical');
	assert.equal(lines.line1, '8A');
	assert.match(lines.line2 ?? '', /m$/);
	assert.equal(mod.libraryKeyWheelLabelLines('8A', 'camelot').line1, '8A');
	assert.throws(() => mod.libraryKeyWheelLabelLines('Q9', 'camelot'), /did not format/);
	assert.throws(() => mod.libraryKeyWheelLabelLines('8A', 'nonsense'));
	for (const seg of mod.libraryKeyWheelSegments()) {
		for (const notation of ['camelot', 'camelot_musical', 'open_key', 'musical']) {
			assert.ok(mod.libraryKeyWheelLabelLines(seg.code, notation).line1.length > 0);
		}
	}
	const openLabel = mod.libraryKeyWheelLabelLines('8A', 'open_key').line1;
	assert.ok(openLabel.length > 0);
});

test('canonicalize, toggle, reset, and reject malformed values', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	assert.deepEqual(mod.canonicalizeLibraryKeyFilter(['9A', '8A']), ['8A', '9A']);
	assert.deepEqual(mod.toggleLibraryKeySelection(['8A'], '8A'), []);
	assert.deepEqual(mod.toggleLibraryKeySelection([], '8A'), ['8A']);
	assert.deepEqual(mod.resetLibraryKeySelection(), []);
	assert.throws(() => mod.canonicalizeLibraryKeyFilter(['8a']));
	assert.throws(() => mod.canonicalizeLibraryKeyFilter(['8A', '8A']));
	assert.throws(() => mod.validateLibraryKeyFilterField({ bad: true }, 'test'));
	assert.throws(() => mod.validateLibraryKeyFilterField(['8A', '8A'], 'test'));
	const all24 = [];
	for (let n = 1; n <= 12; n += 1) {
		all24.push(`${n}B`, `${n}A`);
	}
	assert.throws(() => mod.validateLibraryKeyFilterField([...all24, '1A'], 'test'));
});

test('panel: segments are pressable buttons; Compatible and Reset are real controls', () => {
	assert.match(KEY_FILTER_PANEL, /role="button"/);
	assert.match(KEY_FILTER_PANEL, /aria-pressed=\{selected\}/);
	assert.match(KEY_FILTER_PANEL, /tabindex="0"/);
	assert.match(KEY_FILTER_PANEL, /onkeydown=/);
	assert.match(KEY_FILTER_PANEL, /data-testid="library-key-compatible"[\s\S]*>Compatible</);
	assert.match(KEY_FILTER_PANEL, /data-testid="library-key-reset"[\s\S]*>Reset</);
	assert.match(KEY_FILTER_PANEL, /_commit\(compatibleKeys\)/);
	assert.match(KEY_FILTER_PANEL, /libraryKeyWheelLabelLines\(segment\.code, notation\)/);
	assert.doesNotMatch(KEY_FILTER_PANEL, /role="checkbox"/);
	assert.doesNotMatch(KEY_FILTER_PANEL, /prefs\.svelte/);
});

test('applyLibraryKeyFilter: empty selection passes all; otherwise only selected keys', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	const rows = [{ key: '8A' }, { key: 'Am' }, { key: '9A' }, { key: null }];
	assert.equal(mod.applyLibraryKeyFilter(rows, []), rows);
	assert.deepEqual(mod.applyLibraryKeyFilter(rows, ['8A']), [{ key: '8A' }, { key: 'Am' }]);
});

test('exact match across spellings; neighbours vetoed; keyless excluded', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	const selected = ['8A'];
	assert.equal(mod.rowMatchesLibraryKeyFilter('8A', selected), true);
	assert.equal(mod.rowMatchesLibraryKeyFilter('Am', selected), true);
	assert.equal(mod.rowMatchesLibraryKeyFilter('7A', selected), false);
	assert.equal(mod.rowMatchesLibraryKeyFilter('8B', selected), false);
	assert.equal(mod.rowMatchesLibraryKeyFilter(null, selected), false);
	assert.equal(mod.rowMatchesLibraryKeyFilter('not-a-key', selected), false);
	assert.equal(mod.rowMatchesLibraryKeyFilter('7A', []), true);
});

test('reference: selected row beats playing deck', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	const decks = [
		{ is_master: true, playing: true, key: '5A' },
		{ is_master: false, playing: true, key: '6A' }
	];
	assert.equal(mod.resolveLibraryKeyFilterReference('11A', decks), '11A');
	assert.equal(mod.resolveLibraryKeyFilterReference(null, decks), '5A');
	assert.equal(
		mod.resolveLibraryKeyFilterReference(null, [{ is_master: false, playing: false, key: '3A' }]),
		null
	);
});

test('BrowserPanel keeps key predicate through search fallbacks', () => {
	assert.match(BROWSER_PANEL, /_applyKeyFilterFinal/);
	assert.match(BROWSER_PANEL, /resolveSearchFilterFallback[\s\S]*_applyKeyFilterFinal/);
	assert.match(BROWSER_PANEL, /selectSearchFilterFallback[\s\S]*_applyKeyFilterFinal/);
});

test('library key filter popover uses viewport clamping and is reachable from BrowserPanel', () => {
	assert.match(BROWSER_PANEL, /<LibraryKeyFilterPopover \{selectedRowKey\} decks=\{DECK_IDS\.map\(\(d\) => decks\[d\]\)\} \/>/);
	assert.match(BROWSER_PANEL, /applyLibraryFilters\(rows, uiPrefs\)/);
	assert.match(BROWSER_PANEL, /selectedRowKeyIn\(pane\.selected_id, \[pane\.search_results, pane\.rows\]\)/);
	assert.match(KEY_FILTER_POPOVER, /triggerFloatingAction/);
	assert.match(KEY_FILTER_POPOVER, /aria-haspopup="dialog"/);
});

test('an agent write (ui_prefs event) re-reads the key filter into the open table', () => {
	// $effect returns each unsubscribe, so leaving the page drops the listeners.
	assert.match(KEY_FILTER_POPOVER, /\$effect\(\(\) => subscribeKind\('ui_prefs', rereadLibraryKeyFilter\)\)/);
	// A reconnect only announces a resync; writes missed while offline are re-read then.
	assert.match(KEY_FILTER_POPOVER, /\$effect\(\(\) => subscribeResync\(rereadLibraryKeyFilter\)\)/);
	// Returning to the route re-reads; the first mount leaves it to boot hydration (Codex P2, #7743).
	assert.match(KEY_FILTER_POPOVER, /<script lang="ts" module>[\s\S]*let mountedBefore = false;/);
	assert.match(KEY_FILTER_POPOVER, /if \(mountedBefore\) rereadLibraryKeyFilter\(\);\s*mountedBefore = true;/);
	// A failed re-read stays visible on the trigger (Sol P1, #7743).
	assert.match(KEY_FILTER_POPOVER, /class:sync-error=\{syncError !== null\}/);
	assert.match(KEY_FILTER_POPOVER, /aria-label=\{syncError === null \? undefined : `\$\{triggerLabel\}: \$\{syncError\}`\}/);
	const prefs = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs.svelte.ts'), 'utf8');
	assert.match(prefs, /libraryKeyFilterSync\.error = `Key filter could not be re-read from disk/);
	// Only the latest re-read may change the error (Sol P1, #7743).
	assert.match(prefs, /if \(gen === _libraryKeyFilterRereadGen\) libraryKeyFilterSync\.error = null;/);
	assert.match(prefs, /if \(gen !== _libraryKeyFilterRereadGen\) return;\s*const detail/);
	// A failed PUT releases its unsaved mark and re-reads disk (Codex P2, #7743).
	assert.match(prefs, /if \(_libraryKeyFilterUnsaved\.rev === writeRev\) \{\s*_libraryKeyFilterUnsaved\.rev = 0;\s*_libraryKeyFilterRereadDeferred = true;/);
	// A re-read skipped for an unsaved local write runs again once that write settles.
	assert.match(prefs, /_libraryKeyFilterRereadDeferred && _libraryKeyFilterUnsaved\.rev === 0[\s\S]*rereadLibraryKeyFilter\(\)/);
});

test('an older PUT acknowledgement never reverts a newer selection (Codex P2, #7743)', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/verified-pref-writes.ts');
	const uiPrefs = { compatible_filter: {}, library_watcher_folders: [], library_key_filter: ['8A'], confirm: {} };
	const unsaved = { rev: 1 };
	const puts = [];
	let release = () => {};
	const writers = mod.makeVerifiedPrefWriters({
		uiPrefs,
		persist: () => {},
		sync: (_patch, run) => run(),
		put: (patch) => {
			puts.push(patch);
			return new Promise((resolve) => {
				release = resolve;
			});
		},
		unsavedConfirm: new Map(),
		libraryKeyFilterUnsaved: unsaved,
		markLibraryKeyFilterSaved: (rev) => {
			if (unsaved.rev === rev) unsaved.rev = 0;
		}
	});
	const first = writers.setLibraryKeyFilterDisk(1);
	// A second click lands while the first PUT is in flight.
	uiPrefs.library_key_filter = ['9A'];
	unsaved.rev = 2;
	release();
	await first;
	assert.deepEqual(puts, [{ library_key_filter: ['8A'] }]);
	assert.deepEqual(uiPrefs.library_key_filter, ['9A'], 'the newer click survives');
	assert.equal(unsaved.rev, 2, 'the newer write is still unsaved');
});

test('a disk read superseded by a newer read or a local edit is dropped (Codex P2, #7743)', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-key-filter.ts');
	let rev = 0;
	const guard = mod.makeKeyFilterReadGuard(() => rev);
	const a = guard.begin();
	assert.equal(guard.isCurrent(a), true, 'an undisturbed read applies');
	const b = guard.begin();
	assert.equal(guard.isCurrent(a), false, 'an older read loses to a newer one');
	assert.equal(guard.isCurrent(b), true);
	rev += 1; // a click lands while read b is in flight
	assert.equal(guard.isCurrent(b), false, 'a read that spans a local edit is dropped');
	assert.equal(guard.isCurrent(guard.begin()), true);
	const hydrate = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs-hydrate.ts'), 'utf8');
	assert.match(hydrate, /library_key_filter is missing from the response/);
	assert.match(hydrate, /const token = guard\.begin\(\);[\s\S]*if \(!guard\.isCurrent\(token\)\) return;[\s\S]*?hydrateLibraryKeyFilter\(deps\.uiPrefs/);
});

test('a malformed disk key filter throws instead of keeping the old selection (Sol P1, #7743)', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/verified-pref-writes.ts');
	const uiPrefs = { library_key_filter: ['8A'] };
	assert.throws(() => mod.hydrateLibraryKeyFilter(uiPrefs, { library_key_filter: ['8a'] }, () => false));
	assert.throws(() => mod.hydrateLibraryKeyFilter(uiPrefs, { library_key_filter: 'x' }, () => true));
	assert.throws(() => mod.hydrateLibraryKeyFilter(uiPrefs, {}, () => false), /missing from the response/);
	assert.deepEqual(uiPrefs.library_key_filter, ['8A']);
	mod.hydrateLibraryKeyFilter(uiPrefs, { library_key_filter: ['9A'] }, () => true);
	assert.deepEqual(uiPrefs.library_key_filter, ['8A'], 'an unsaved local write wins');
	mod.hydrateLibraryKeyFilter(uiPrefs, { library_key_filter: ['9A'] }, () => false);
	assert.deepEqual(uiPrefs.library_key_filter, ['9A']);
});

test('the selected row key is found in search results, then in the rows a fallback shows (Sol P1, #7743)', async () => {
	const c = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	const results = [{ stable_id: 'a', key: '8A' }];
	const rows = [{ stable_id: 'b', key: '3B' }];
	assert.equal(c.selectedRowKeyIn('a', [results, rows]), '8A');
	assert.equal(c.selectedRowKeyIn('b', [results, rows]), '3B', 'a fallback row is still found');
	assert.equal(c.selectedRowKeyIn('z', [results, rows]), null);
	assert.equal(c.selectedRowKeyIn(null, [results, rows]), null);
});

test('hydration skips library_key_filter while a disk write is pending', () => {
	const verified = readFileSync(
		join(FRONTEND_ROOT, 'src/lib/rb/verified-pref-writes.ts'),
		'utf8'
	);
	assert.match(verified, /hydrateLibraryKeyFilter[\s\S]*isUnsaved/);
});
