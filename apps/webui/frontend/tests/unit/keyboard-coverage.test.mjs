// requirement: KEYB-13737
/**
 * Keyboard coverage report and ratchet (#13689, epic #10346).
 *
 * Regression lines:
 * - if a KEYMAP_COMMANDS id with no opendj chord is missing from noChord.opendj then broken
 * - if a registry command with no palette row is missing from noPaletteRow then broken
 * - if a gap count rises above its ceiling without the test failing and naming the ids then broken
 * - if the report does not round-trip through JSON unchanged then broken
 *
 * Ratchet: when a PR closes gaps, lower the matching ceiling below to the printed count.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Counts on main 3b329a35a08 (Fri 13 Nov 2026). Gap buckets may only fall; performanceLetterDigitChords may only rise. */
const CEILINGS = {
	noChord: { opendj: 193, rekordbox: 165, serato: 155 },
	noPaletteRow: 200,
	noKeyboardPath: 181,
	performanceLetterDigitChords: 43
};

let coverage;
let presets;
let report;

before(async () => {
	coverage = await loadTypeScriptModule('src/lib/keyboard/coverage.ts');
	presets = await loadTypeScriptModule('src/lib/keyboard/presets/index.ts');
	report = coverage.keyboardCoverageReport();
	console.log(JSON.stringify({ keyboardCoverage: report.counts }));
});

function sourcesWith(extra) {
	const sources = coverage.shippedCoverageSources();
	return { ...sources, commands: [...sources.commands, ...extra] };
}

// [if] a KEYMAP_COMMANDS id has no chord in the opendj preset [then] it is listed under noChord.opendj, [else stop].
test('noChord.opendj lists exactly the keymap commands the opendj preset leaves unbound', () => {
	const unbound = presets.unboundCommands('opendj').map((c) => c.id);
	const keymapIds = new Set(
		coverage
			.shippedCoverageSources()
			.commands.filter((c) => c.source === 'keymap')
			.map((c) => c.id)
	);
	assert.deepEqual(
		report.noChord.opendj.filter((id) => keymapIds.has(id)),
		unbound
	);
	assert.ok(report.noChord.opendj.includes('deck.1.play'));
	assert.ok(!report.noChord.opendj.includes('transport.toggle'));
	assert.ok(
		!report.noChord.opendj.includes('dial.position.1'),
		'a registry defaultKey counts as a chord'
	);
});

// [if] a registry command has no palette row [then] it is listed under noPaletteRow, [else stop].
test('noPaletteRow lists registry and keymap commands the Cmd-K bar does not offer', () => {
	for (const id of ['selector.move.up', 'selector.drill-in', 'preset.run.bass-swap-8']) {
		assert.ok(report.noPaletteRow.includes(id), `${id} has no palette row`);
	}
	for (const id of ['dial.position.1', 'dial.jump.9', 'deck.3.play', 'wave.zoom_in']) {
		assert.ok(!report.noPaletteRow.includes(id), `${id} has a palette row`);
	}
	const injected = coverage.buildKeyboardCoverageReport(
		sourcesWith([
			{
				id: 'coverage.probe.unoffered',
				source: 'registry',
				scopes: ['global'],
				defaultKeys: ['Home']
			}
		])
	);
	assert.ok(injected.noPaletteRow.includes('coverage.probe.unoffered'));
	assert.ok(!injected.noChord.opendj.includes('coverage.probe.unoffered'));
});

// [if] any count rises above its ceiling [then] the test fails and names the new ids, [else stop].
test('shipped counts stay within the ratchet ceilings and a new gap names its id', () => {
	const breaches = coverage.coverageBreaches(report, CEILINGS);
	assert.deepEqual(
		breaches.map((b) => `${b.bucket} ${b.count} > ${b.ceiling}`),
		[],
		`keyboard coverage regressed:\n${breaches.map((b) => `${b.bucket}: ${b.ids.join(', ')}`).join('\n')}`
	);
	const grown = coverage.buildKeyboardCoverageReport(
		sourcesWith([{ id: 'deck.9.brake', source: 'keymap', scopes: ['deck'], defaultKeys: [] }])
	);
	const grownBreaches = coverage.coverageBreaches(grown, CEILINGS);
	assert.deepEqual(
		grownBreaches.map((b) => b.bucket),
		['noChord.opendj', 'noChord.rekordbox', 'noChord.serato', 'noPaletteRow', 'noKeyboardPath']
	);
	for (const breach of grownBreaches) assert.ok(breach.ids.includes('deck.9.brake'), breach.bucket);
});

// [if] the report is serialised [then] it is plain JSON with the counts and id lists, [else stop].
test('report round-trips through JSON and every count equals its id list length', () => {
	assert.deepEqual(JSON.parse(JSON.stringify(report)), report);
	for (const preset of presets.KEYMAP_PRESET_IDS) {
		assert.equal(report.counts.noChord[preset], report.noChord[preset].length);
	}
	assert.equal(report.counts.noPaletteRow, report.noPaletteRow.length);
	assert.equal(report.counts.noKeyboardPath, report.noKeyboardPath.length);
	assert.equal(report.counts.performanceLetterDigitChords, report.performanceShortcuts.length);
	assert.ok(
		report.performanceShortcuts.some((s) => s.chord === 'Shift+K' && s.action === 'tempo-reset')
	);
});

// [if] an alias names a palette id that no longer exists [then] building the report throws, [else stop].
test('a stale palette alias fails loud', () => {
	const sources = coverage.shippedCoverageSources();
	const stale = { ...sources, aliases: { ...sources.aliases, 'deck.*.play': 'deck.gone' } };
	assert.throws(() => coverage.buildKeyboardCoverageReport(stale), /unknown palette id deck\.gone/);
});
