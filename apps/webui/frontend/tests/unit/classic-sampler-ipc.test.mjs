// requirement: SAMPLER-14719
/**
 * Issues #9987 and #9989 (agent parity): the classic sampler unit's sampler_unit_* bus commands
 * are parsed with exact keys and run the classic store's own actions (one code path).
 *
 * Regression lines:
 * - if a good sampler_unit_* payload is refused then agents cannot reach the unit
 * - if a bank, slot, mode or gain outside the unit parses then the bus guesses
 * - if a load names both a track and a file, or stop names half an address, it parses
 * - if a bus load runs before the banks were read then the PUT drops the stored slots
 * - if trigger held:false does not release then a gate slot hangs for agents
 * - if an empty slot's trigger resolves then the bus never reports the refusal
 * - if the performance bus does not route sampler_unit_* to the unit then the dispatch never reaches it
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export * from '$lib/player/sampler/classic-sampler.svelte';",
	"export { applyClassicSamplerCommand } from '$lib/player/sampler/classic-sampler-ipc';"
].join('\n');

let g;
before(async () => {
	g = await loadTypeScriptModule('src/lib/player/sampler/classic-sampler-ipc-guards.ts');
});

const GOOD = [
	{ type: 'sampler_unit_load', bank: 0, slot: 0, stable_id: 't1', title: 'One' },
	{ type: 'sampler_unit_load', bank: 3, slot: 15, path: '/a.wav', duration_ms: 750 },
	{ type: 'sampler_unit_load', bank: 1, slot: 2 },
	{ type: 'sampler_unit_trigger', bank: 2, slot: 7 },
	{ type: 'sampler_unit_trigger', bank: 2, slot: 7, held: false },
	{ type: 'sampler_unit_stop', bank: 0, slot: 1 },
	{ type: 'sampler_unit_stop' },
	{ type: 'sampler_unit_mode', bank: 0, slot: 1, mode: 'gate' },
	{ type: 'sampler_unit_gain', bank: 0, slot: 1, gain_db: -24 },
	{ type: 'sampler_unit_master_gain', gain_db: -60 }
];

const BAD = [
	[{ type: 'sampler_unit_trigger', bank: 4, slot: 0 }, /bank must be 0..3/],
	[{ type: 'sampler_unit_trigger', bank: 0, slot: 1.5 }, /slot must be 0..15/],
	[{ type: 'sampler_unit_trigger', bank: 0, slot: 0, ref: 'S1-01' }, /unexpected fields: ref/],
	[{ type: 'sampler_unit_trigger', bank: 0, slot: 0, held: 1 }, /held must be boolean/],
	[{ type: 'sampler_unit_load', bank: 0, slot: 0, stable_id: 't', path: '/a' }, /not both/],
	[{ type: 'sampler_unit_load', bank: 0, slot: 0, path: '' }, /path must be a non-empty/],
	[
		{ type: 'sampler_unit_load', bank: 0, slot: 0, path: '/a', duration_ms: 0 },
		/duration_ms must be/
	],
	[{ type: 'sampler_unit_stop', bank: 0 }, /numeric bank and slot/],
	[{ type: 'sampler_unit_mode', bank: 0, slot: 0, mode: 'reverse' }, /mode must be one of/],
	[{ type: 'sampler_unit_gain', bank: 0, slot: 0, gain_db: 7 }, /-24..6 dB/],
	[{ type: 'sampler_unit_master_gain', gain_db: 1 }, /-60..0 dB/]
];

test('good sampler_unit payloads parse to themselves', () => {
	/** [if] a well-formed unit command is refused or rewritten [then] agents cannot reach that action, [else stop]. */
	for (const cmd of GOOD) {
		assert.equal(g.isClassicSamplerCommandType(cmd.type), true);
		assert.deepEqual(g.parseClassicSamplerCommand(cmd), cmd);
	}
	assert.equal(g.isClassicSamplerCommandType('sampler_trigger'), false);
});

test('out-of-unit addresses, both sources, half a stop address and bad domains are refused', () => {
	/** [if] any malformed unit payload parses [then] the bus guesses what the agent meant, [else stop]. */
	for (const [cmd, why] of BAD) assert.throws(() => g.parseClassicSamplerCommand(cmd), why);
});

/** A fetch answering GET /banks with one stored slot and echoing each PUT as the new view. */
function banksServer() {
	const calls = [];
	let stored = {
		v: 1,
		active_bank: 0,
		banks: Array.from({ length: 4 }, () => Array(16).fill(null))
	};
	stored.banks[2][0] = {
		path: '/kept.wav',
		mode: 'one_shot',
		gain_db: 0,
		title: 'kept',
		missing: false
	};
	const fetchFn = async (url, init) => {
		calls.push(init.method);
		if (init.method === 'PUT') {
			const doc = JSON.parse(init.body);
			stored = {
				...doc,
				banks: doc.banks.map((b) => b.map((s) => (s === null ? null : { ...s, missing: false })))
			};
		}
		return new Response(JSON.stringify(stored), { status: 200 });
	};
	return { calls, fetchFn, stored: () => stored };
}

function engineRecorder() {
	const calls = [];
	return {
		calls,
		engine: {
			toggle: async (slot) => void calls.push(['toggle', slot.slot_id, slot.settings.mode]),
			stop: (id) => void calls.push(['stop', id]),
			preview: async () => undefined,
			stopPreview: () => undefined,
			reside: () => undefined,
			phase: () => 'free',
			playhead: () => null
		}
	};
}

test('a bus load reads the stored banks first, then writes the new slot beside them', async () => {
	/** [if] sampler_unit_load runs on a fresh store [then] GET then PUT, the stored slot kept, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const server = banksServer();
	S.setClassicSamplerPorts({ fetchFn: server.fetchFn });
	await S.applyClassicSamplerCommand({
		type: 'sampler_unit_load',
		bank: 0,
		slot: 0,
		stable_id: 't1'
	});
	assert.deepEqual(server.calls, ['GET', 'PUT']);
	assert.equal(server.stored().banks[2][0].title, 'kept');
	assert.equal(S.classicSampler.unit.banks[0][0].source.stable_id, 't1');
});

test('trigger, release, stop, mode and gains run the store actions a pad runs', async () => {
	/** [if] a gate press and held:false release, a stop all and an empty-slot trigger run [then] the engine sees them, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const server = banksServer();
	const rec = engineRecorder();
	S.setClassicSamplerPorts({ fetchFn: server.fetchFn, engine: rec.engine });
	const a = { bank: 2, slot: 0 };
	await S.applyClassicSamplerCommand({
		type: 'sampler_unit_load',
		...a,
		path: '/k.wav',
		duration_ms: 800
	});
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_mode', ...a, mode: 'gate' });
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_gain', ...a, gain_db: -6 });
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_master_gain', gain_db: -12 });
	assert.deepEqual(
		[S.classicSampler.unit.banks[2][0].mode, S.classicSampler.unit.banks[2][0].gain_db],
		['gate', -6]
	);
	assert.equal(S.classicSampler.master_gain_db, -12);
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_trigger', ...a });
	const id = rec.calls[0][1];
	assert.deepEqual(rec.calls[0], ['toggle', id, 'gate']);
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_trigger', ...a, held: false });
	assert.deepEqual(rec.calls.at(-1), ['stop', id]);
	await S.applyClassicSamplerCommand({ type: 'sampler_unit_stop' });
	assert.deepEqual(rec.calls.at(-1), ['stop', id]);
	await assert.rejects(
		S.applyClassicSamplerCommand({ type: 'sampler_unit_trigger', bank: 0, slot: 3 }),
		/bank A slot 4 is empty/
	);
	await S.flushClassicBanks();
});

test('the performance bus routes sampler_unit commands through the unit guard', async () => {
	/** [if] dispatchPerformanceCommand skips the unit guard or queues it on a deck [then] agents cannot drive it, [else stop]. */
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'sampler_unit_trigger', bank: 4, slot: 0 }),
			/bank must be 0..3/
		);
		assert.equal(
			ipc.performanceCommandQueueScopes({ type: 'sampler_unit_master_gain', gain_db: 0 }),
			null
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
