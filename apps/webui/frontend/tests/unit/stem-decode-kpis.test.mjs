import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import {
	agreeLine,
	aliasTwoPartBundle,
	appendLedger,
	independenceHolds,
	kpiId,
	laneTimingsToRows,
	missingFixtureMessage
} from '../live/stem-decode-kpis.mjs';

test('aliasTwoPartBundle maps vocals and drums-as-instrumental', () => {
	const vocals = Buffer.from('vocals-audio');
	const drums = Buffer.from('drums-audio');
	const two = aliasTwoPartBundle({ vocals, drums, bass: Buffer.from('b'), other: Buffer.from('o') });
	assert.equal(two.vocals, vocals);
	assert.equal(two.instrumental, drums);
});

test('aliasTwoPartBundle throws when drums is missing', () => {
	assert.throws(
		() => aliasTwoPartBundle({ vocals: Buffer.from('v'), bass: Buffer.from('b') }),
		/missing drums/
	);
});

test('independenceHolds is true only when widths settle independently', () => {
	const goodWorkers = {
		lane4AfterFour: 'workers',
		lane2AfterFour: null,
		lane2AfterTwo: 'workers',
		lane4AfterTwo: 'workers'
	};
	const goodMain = {
		lane4AfterFour: 'workers',
		lane2AfterFour: null,
		lane2AfterTwo: 'main-thread',
		lane4AfterTwo: 'workers'
	};
	assert.equal(independenceHolds(goodWorkers), true);
	assert.equal(independenceHolds(goodMain), true);

	assert.equal(
		independenceHolds({ ...goodMain, lane2AfterFour: 'workers' }),
		false,
		'4-part verdict leaked onto width 2'
	);
	assert.equal(
		independenceHolds({ ...goodMain, lane2AfterTwo: null }),
		false,
		'2-part never settled'
	);
	assert.equal(
		independenceHolds({ ...goodMain, lane4AfterTwo: 'main-thread' }),
		false,
		'4-part verdict changed after 2-part calibration'
	);
	assert.equal(
		independenceHolds({ ...goodMain, lane4AfterFour: null }),
		false,
		'4-part never settled'
	);
});

test('agreeLine applies stopwatchLane with the caller margin', () => {
	const margin = 1.25;
	assert.equal(agreeLine({ chosenLane: 'workers', baselineMs: 171, workerMs: 100, margin }).agrees, true);
	assert.equal(agreeLine({ chosenLane: 'workers', baselineMs: 110, workerMs: 100, margin }).agrees, false);
	assert.equal(agreeLine({ chosenLane: 'main-thread', baselineMs: 51, workerMs: 100, margin }).agrees, true);
});

test('kpiId names per-width per-engine lane timings', () => {
	assert.equal(
		kpiId({ width: 2, engine: 'webkit', lane: 'main-thread' }),
		'stem_decode_2way_ms_webkit_main'
	);
	assert.equal(
		kpiId({ width: 4, engine: 'chromium', lane: 'workers' }),
		'stem_decode_4way_ms_chromium_workers'
	);
	assert.throws(() => kpiId({ width: 3, engine: 'webkit', lane: 'workers' }), /unknown stem decode width/);
});

test('laneTimingsToRows emits eight rows with shared capture metadata', () => {
	const rows = laneTimingsToRows(
		[
			{
				engine: 'webkit',
				widths: {
					4: { baselineMs: 900, workerMs: 500, chosenLane: 'workers', agrees: true },
					2: { baselineMs: 400, workerMs: 300, chosenLane: 'main-thread', agrees: true }
				}
			},
			{
				engine: 'chromium',
				widths: {
					4: { baselineMs: 260, workerMs: 400, chosenLane: 'main-thread', agrees: true },
					2: { baselineMs: 120, workerMs: 150, chosenLane: 'main-thread', agrees: true }
				}
			}
		],
		{
			date: '2026-09-11',
			round: 'issue-2057',
			machine: 'test-host',
			source: 'pnpm test:live:stem-decode-workers PERF-STEMDEC-02, <N> distinct 44.1kHz FLACs, Playwright <engine>, warm pool, decode only',
			captureId: 'issue-2057-stemdec-02',
			fixtureCount: 4
		}
	);
	assert.equal(rows.length, 8);
	for (const row of rows) {
		assert.equal(row.round, 'issue-2057');
		assert.equal(row.unit, 'ms');
		assert.equal(row.capture_id, 'issue-2057-stemdec-02');
		assert.equal(typeof row.value, 'number');
	}
	assert.ok(rows.some((r) => r.kpi === 'stem_decode_2way_ms_chromium_main'));
});

test('appendLedger keeps existing entries and adds new ones', () => {
	const dir = mkdtempSync(path.join(tmpdir(), 'kpi-ledger-'));
	const ledgerPath = path.join(dir, 'kpi-ledger.json');
	writeFileSync(
		ledgerPath,
		`${JSON.stringify({ schema_version: 2, entries: [{ kpi: 'existing', value: 1 }] }, null, 2)}\n`
	);
	const rows = [{ kpi: 'new', value: 2, unit: 'ms' }];
	appendLedger(ledgerPath, rows);
	const ledger = JSON.parse(readFileSync(ledgerPath, 'utf8'));
	assert.equal(ledger.entries.length, 2);
	assert.equal(ledger.entries[0].kpi, 'existing');
	assert.equal(ledger.entries[1].kpi, 'new');
});

test('appendLedger refuses empty rows', () => {
	const dir = mkdtempSync(path.join(tmpdir(), 'kpi-ledger-empty-'));
	const ledgerPath = path.join(dir, 'kpi-ledger.json');
	writeFileSync(ledgerPath, `${JSON.stringify({ schema_version: 2, entries: [] }, null, 2)}\n`);
	assert.throws(() => appendLedger(ledgerPath, []), /empty ledger rows/);
});

test('missingFixtureMessage names Q18_FLAC_DIR', () => {
	const lines = missingFixtureMessage({ found: 0, required: 4, fixtureDir: '/tmp/missing' });
	assert.ok(lines.some((line) => line.includes('Q18_FLAC_DIR')));
});
