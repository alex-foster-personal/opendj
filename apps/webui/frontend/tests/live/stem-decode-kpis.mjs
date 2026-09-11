/**
 * Pure helpers shared by the live stem-decode runner and its unit suite.
 *
 * No Playwright. The only filesystem touch is appendLedger, which the runner
 * calls after a successful live measurement.
 */

import { readFileSync, writeFileSync } from 'node:fs';

import { stopwatchLane } from './lane-oracle.mjs';

export const FOUR_PARTS = ['vocals', 'drums', 'bass', 'other'];
export const TWO_PARTS = ['vocals', 'instrumental'];

/**
 * Build a 2-part byte map from the same four FLAC fixtures as the 4-part arm.
 *
 * @param {Record<string, Buffer>} byPart four-part map keyed by demucs4 names
 * @returns {{ vocals: Buffer, instrumental: Buffer }}
 */
export function aliasTwoPartBundle(byPart) {
	if (byPart.vocals === undefined) {
		throw new Error('missing vocals fixture for 2-part alias');
	}
	if (byPart.drums === undefined) {
		throw new Error('missing drums fixture for instrumental alias');
	}
	return {
		vocals: byPart.vocals,
		instrumental: byPart.drums
	};
}

/**
 * Whether per-layout lane verdicts stayed independent through calibration.
 *
 * @param {{ lane4AfterFour: string | null, lane2AfterFour: string | null, lane2AfterTwo: string | null, lane4AfterTwo: string | null }} lanes
 * @returns {boolean}
 */
export function independenceHolds(lanes) {
	const { lane4AfterFour, lane2AfterFour, lane2AfterTwo, lane4AfterTwo } = lanes;
	if (lane4AfterFour === null) return false;
	if (lane2AfterFour !== null) return false;
	if (lane2AfterTwo === null) return false;
	if (lane4AfterTwo !== lane4AfterFour) return false;
	return true;
}

/**
 * Stopwatch-vs-calibration line using the implementation margin.
 *
 * @param {{ chosenLane: string, baselineMs: number, workerMs: number, margin: number }} opts
 * @returns {{ faster: number, stopwatchSays: string, agrees: boolean, text: string }}
 */
export function agreeLine({ chosenLane, baselineMs, workerMs, margin }) {
	const faster = baselineMs / workerMs;
	const stopwatchSays = stopwatchLane(faster, margin);
	const agrees = chosenLane === stopwatchSays;
	return {
		faster,
		stopwatchSays,
		agrees,
		text:
			`  calibration chose ${chosenLane}; stopwatch says ${stopwatchSays}` +
			` (workers ${faster.toFixed(2)}x vs a ${margin}x margin)` +
			` -> ${agrees ? 'AGREE' : 'DISAGREE'}`
	};
}

/**
 * @param {{ width: 2 | 4, engine: 'webkit' | 'chromium', lane: 'main-thread' | 'workers' }} opts
 * @returns {string}
 */
export function kpiId({ width, engine, lane }) {
	const widthLabel = width === 2 ? '2way' : width === 4 ? '4way' : null;
	if (widthLabel === null) {
		throw new Error(`unknown stem decode width: ${width}`);
	}
	if (engine !== 'webkit' && engine !== 'chromium') {
		throw new Error(`unknown engine: ${engine}`);
	}
	const laneLabel = lane === 'main-thread' ? 'main' : lane === 'workers' ? 'workers' : null;
	if (laneLabel === null) {
		throw new Error(`unknown lane: ${lane}`);
	}
	return `stem_decode_${widthLabel}_ms_${engine}_${laneLabel}`;
}

/**
 * @param {Array<{ engine: string, widths: Record<number, { baselineMs: number, workerMs: number, chosenLane: string, agrees: boolean }> }>} engineResults
 * @param {{ date: string, round: string, machine: string, source: string, captureId: string, fixtureCount: number }} meta
 * @returns {object[]}
 */
export function laneTimingsToRows(engineResults, meta) {
	const rows = [];
	for (const { engine, widths } of engineResults) {
		for (const width of [4, 2]) {
			const w = widths[width];
			const otherMs = {
				'main-thread': w.workerMs,
				workers: w.baselineMs
			};
			for (const lane of ['main-thread', 'workers']) {
				const value = lane === 'main-thread' ? w.baselineMs : w.workerMs;
				rows.push({
					date: meta.date,
					round: meta.round,
					kpi: kpiId({ width, engine, lane }),
					value,
					unit: 'ms',
					machine: meta.machine,
					source: meta.source.replace('<engine>', engine).replace('<N>', String(meta.fixtureCount)),
					capture_id: meta.captureId,
					note:
						`${width}-part ${engine} ${lane}: ${value}ms, other lane ${otherMs[lane]}ms, ` +
						`calibration ${w.chosenLane}, ${w.agrees ? 'AGREE' : 'DISAGREE'}`
				});
			}
		}
	}
	return rows;
}

/**
 * @param {string} ledgerPath
 * @param {object[]} rows
 */
export function appendLedger(ledgerPath, rows) {
	if (!rows || rows.length === 0) {
		throw new Error('refusing to append empty ledger rows');
	}
	const ledger = JSON.parse(readFileSync(ledgerPath, 'utf8'));
	if (!Array.isArray(ledger.entries)) {
		throw new Error(`ledger at ${ledgerPath} has no entries array`);
	}
	ledger.entries.push(...rows);
	writeFileSync(ledgerPath, `${JSON.stringify(ledger, null, 2)}\n`);
}

/**
 * Lines printed when fewer than the required FLAC fixtures exist.
 *
 * @param {{ found: number, required: number, fixtureDir: string, envVar?: string }} opts
 * @returns {string[]}
 */
export function missingFixtureMessage({ found, required, fixtureDir, envVar = 'Q18_FLAC_DIR' }) {
	return [
		`need ${required} distinct .flac files, found ${found} in ${fixtureDir}`,
		`point this run at a directory: ${envVar}=/abs/dir pnpm test:live:stem-decode-workers`
	];
}
