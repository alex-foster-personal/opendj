/**
 * Issue #2061 / DECKUX-16: double-click tempo edit modal - pure math and
 * source-scan wiring tests.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

function readSrc(relPath) {
	return readFileSync(`${FRONTEND_ROOT}/${relPath}`, 'utf8');
}

let tempoEdit;

before(async () => {
	tempoEdit = await loadTypeScriptModule('src/lib/rb/tempo-edit.ts');
});

describe('tempo-edit pure helpers', () => {
	test('parseTempoBpmInput accepts valid BPM and rejects invalid', () => {
		const { parseTempoBpmInput } = tempoEdit;
		assert.equal(parseTempoBpmInput('130'), 130);
		assert.equal(parseTempoBpmInput('128.50'), 128.5);
		assert.equal(parseTempoBpmInput(' 128 '), 128);
		assert.equal(parseTempoBpmInput(''), null);
		assert.equal(parseTempoBpmInput('abc'), null);
		assert.equal(parseTempoBpmInput('0'), null);
		assert.equal(parseTempoBpmInput('-1'), null);
	});

	test('tempoWritesFromTargetBpm emits tempo write for in-range BPM', () => {
		const { tempoWritesFromTargetBpm } = tempoEdit;
		const writes = tempoWritesFromTargetBpm({
			deck: 1,
			targetBpm: 130,
			baseBpm: 128,
			currentRange: 16
		});
		assert.deepEqual(writes, [{ type: 'tempo', deck: 1, ratio: 130 / 128 }]);
	});

	test('range expand prepends pitch_range when needed', () => {
		const { tempoWritesFromTargetBpm } = tempoEdit;
		const wide = tempoWritesFromTargetBpm({
			deck: 1,
			targetBpm: 150,
			baseBpm: 128,
			currentRange: 16
		});
		assert.deepEqual(wide, [
			{ type: 'pitch_range', deck: 1, range: 100 },
			{ type: 'tempo', deck: 1, ratio: 150 / 128 }
		]);
		const inRange = tempoWritesFromTargetBpm({
			deck: 1,
			targetBpm: 130,
			baseBpm: 128,
			currentRange: 16
		});
		assert.deepEqual(inRange, [{ type: 'tempo', deck: 1, ratio: 130 / 128 }]);
		const expand16 = tempoWritesFromTargetBpm({
			deck: 1,
			targetBpm: 139,
			baseBpm: 128,
			currentRange: 8
		});
		assert.deepEqual(expand16, [
			{ type: 'pitch_range', deck: 1, range: 16 },
			{ type: 'tempo', deck: 1, ratio: 139 / 128 }
		]);
	});

	test('WIDE overflow returns null', () => {
		const { tempoWritesFromTargetBpm } = tempoEdit;
		assert.equal(
			tempoWritesFromTargetBpm({
				deck: 1,
				targetBpm: 400,
				baseBpm: 128,
				currentRange: 100
			}),
			null
		);
	});

	test('halveDoubleVisibility thresholds', () => {
		const { halveDoubleVisibility } = tempoEdit;
		assert.deepEqual(halveDoubleVisibility(150), { halve: true, double: false });
		assert.deepEqual(halveDoubleVisibility(149.99), { halve: false, double: false });
		assert.deepEqual(halveDoubleVisibility(100), { halve: false, double: true });
		assert.deepEqual(halveDoubleVisibility(75), { halve: false, double: true });
		assert.deepEqual(halveDoubleVisibility(74.99), { halve: false, double: false });
		assert.deepEqual(halveDoubleVisibility(101), { halve: false, double: false });
	});

	test('nudgeBpm arithmetic', () => {
		const { nudgeBpm } = tempoEdit;
		assert.equal(nudgeBpm(128, 1), 129);
		assert.equal(nudgeBpm(128, -1), 127);
		assert.equal(nudgeBpm(128, 5), 133);
		assert.equal(nudgeBpm(128, -5), 123);
	});
});

describe('DeckHeader dblclick wiring', () => {
	const header = readSrc('src/lib/components/rb/deck/DeckHeader.svelte');

	test('dblclick on tempo readout opens TempoEditModal without dispatching tempo', () => {
		assert.match(header, /onTempoReadoutDblClick|ondblclick/);
		assert.match(header, /class="bpm"/);
		assert.match(header, /TempoEditModal/);
		assert.match(header, /clientX/);
		assert.match(header, /clientY/);
		assert.match(header, /tempoEditAt/);
		// The handler itself must not dispatch: scoped to the dblclick handler, since the
		// header dispatches other commands (the key nudge step row, DECKGAP-7691b) elsewhere.
		const start = header.indexOf('function onTempoReadoutDblClick(');
		assert.notEqual(start, -1, 'no onTempoReadoutDblClick handler');
		const handler = header.slice(start, header.indexOf('\n\t}\n', start));
		assert.doesNotMatch(handler, /runPerformanceCommandFromUi|onResetTempo|type: 'tempo'/);
		assert.doesNotMatch(header, /ondblclick=\{[^}]*runPerformanceCommandFromUi/);
	});

	test('dblclick no-ops when unloaded and uses preventDefault/stopPropagation', () => {
		assert.match(header, /stable_id === null/);
		assert.match(header, /preventDefault/);
		assert.match(header, /stopPropagation/);
	});
});

// Codex P2 on PR #6235: a refused tempo write resolves ok: false instead of
// throwing, so a TAP that changed nothing must not read as applied.
describe('runTempoWrites', () => {
	const writes = [
		{ type: 'pitch_range', deck: 1, range: 16 },
		{ type: 'tempo', deck: 1, ratio: 1.1 }
	];
	test('a refused write reports false and stops the rest', async () => {
		const sent = [];
		const taken = await tempoEdit.runTempoWrites(writes, async (write) => {
			sent.push(write.type);
			return { ok: false };
		});
		assert.equal(taken, false);
		assert.deepEqual(sent, ['pitch_range'], 'no tempo against a refused range');
	});
	test('control: every write taken reports true', async () => {
		const sent = [];
		const taken = await tempoEdit.runTempoWrites(writes, async (write) => {
			sent.push(write.type);
			return { ok: true };
		});
		assert.equal(taken, true);
		assert.deepEqual(sent, ['pitch_range', 'tempo']);
	});
	test('the modal reports what the writes returned', () => {
		const modal = readSrc('src/lib/components/rb/deck/TempoEditModal.svelte');
		assert.match(modal, /const taken = await runTempoWrites\(writes, \(write\) => runPerformanceCommandFromUi\(write, pressT0Ms\)\);/);
		assert.match(modal, /return taken;/);
		assert.match(modal, /tapApplied = await _dispatchWrites\(id, bpm, event\.timeStamp\);/);
	});
});

describe('TempoEditModal commit path', () => {
	const modal = readSrc('src/lib/components/rb/deck/TempoEditModal.svelte');

	test('commits through runPerformanceCommandFromUi and tempoWritesFromTargetBpm', () => {
		assert.match(modal, /runPerformanceCommandFromUi/);
		assert.match(modal, /type: 'tempo'/);
		assert.match(modal, /tempoWritesFromTargetBpm/);
		assert.doesNotMatch(modal, /engine\.setTempoRatio/);
	});

	test('arrow keys, halve/double, slider, and context controls', () => {
		assert.match(modal, /ArrowUp/);
		assert.match(modal, /ArrowDown/);
		assert.match(modal, /shiftKey/);
		assert.match(modal, /halveDoubleVisibility/);
		assert.match(modal, /200px/);
		assert.match(modal, /BEAT SYNC/);
		assert.match(modal, /MASTER/);
		assert.match(modal, /sync_mode/);
		assert.match(modal, /key_nudge/);
		assert.match(modal, /master_tempo/);
	});
});
