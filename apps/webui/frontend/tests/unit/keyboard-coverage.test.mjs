// requirement: KEYB-13737, KEYB-14823
/**
 * Keyboard coverage report and ratchet (#13689, epic #10346).
 *
 * Regression lines:
 * - if a KEYMAP_COMMANDS id with no opendj chord is missing from noChord.opendj then broken
 * - if the report grows a Cmd-K bucket again (every command is a Cmd-K row since #13797) then broken
 * - if a gap count rises above its ceiling without the test failing and naming the ids then broken
 * - if the report does not round-trip through JSON unchanged then broken
 *
 * Ratchet: when a PR closes gaps, lower the matching ceiling below to the printed count.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Counts with #13797 and #13737 combined (Fri 13 Nov 2026). Gap buckets may only fall; performanceLetterDigitChords may only rise. */
const CEILINGS = {
	noChord: { opendj: 193, rekordbox: 165, serato: 155 },
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

// [if] the report is built [then] it measures chords only: every keymap and registry command is a
// Cmd-K row through palette/keyboard-command-rows.ts (#13797), so a Cmd-K bucket could never move, [else stop].
test('the report has no Cmd-K bucket, only per-preset chord gaps', () => {
	assert.deepEqual(Object.keys(report).sort(), ['counts', 'noChord', 'performanceShortcuts']);
	assert.deepEqual(Object.keys(report.counts).sort(), [
		'keymapCommands',
		'noChord',
		'performanceLetterDigitChords',
		'registryCommands'
	]);
	assert.equal('aliases' in coverage.shippedCoverageSources(), false);
	assert.equal('palette' in coverage.shippedCoverageSources(), false);
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
		['noChord.opendj', 'noChord.rekordbox', 'noChord.serato']
	);
	for (const breach of grownBreaches) assert.ok(breach.ids.includes('deck.9.brake'), breach.bucket);
});

// [if] the report is serialised [then] it is plain JSON with the counts and id lists, [else stop].
test('report round-trips through JSON and every count equals its id list length', () => {
	assert.deepEqual(JSON.parse(JSON.stringify(report)), report);
	for (const preset of presets.KEYMAP_PRESET_IDS) {
		assert.equal(report.counts.noChord[preset], report.noChord[preset].length);
	}
	assert.equal(report.counts.performanceLetterDigitChords, report.performanceShortcuts.length);
	assert.ok(
		report.performanceShortcuts.some((s) => s.chord === 'Shift+K' && s.action === 'tempo-reset')
	);
});
