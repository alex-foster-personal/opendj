/**
 * KEYSYNC-01: the Rust page route decides key sync and absolute key shifts,
 * then speaks the native key_nudge/master_tempo commands.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let camelot;
let ScopedCommandScheduler;
let sent;
let notBuilt;
let toasts;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
	camelot = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
	({ ScopedCommandScheduler } = await loadTypeScriptModule(
		'src/lib/rb/performance-command-scheduler.ts'
	));
});

beforeEach(() => {
	sent = [];
	toasts = [];
	notBuilt = new Set(['key_sync', 'key_nudge', 'stem_mute']);
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt,
		send: (command) => {
			sent.push(command);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: () => null
	});
	m.rustMode.enabled = true;
	m.rustMode.keyLock = true;
	m.rustMaster.deck = null;
	m.rustMaster.mode = 'auto';
	m.link.toast = (message, kind) => toasts.push({ message, kind });
	m.resetRustKeySyncForTest();
	for (const deck of [1, 2, 3, 4]) {
		Object.assign(m.deckStates[deck], {
			stable_id: null,
			key: null,
			key_shift_semitones: 0,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			key_sync_mode: 'exact',
			key_sync_target: null,
			is_master: false,
			pitch: 1,
			playing: false,
			audible: false,
			transport_pending: false
		});
	}
});

after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});

function seed(deck, values) {
	Object.assign(m.deckStates[deck], { stable_id: `track-${deck}`, ...values });
}

async function waitFor(predicate, message) {
	for (let attempt = 0; attempt < 50; attempt += 1) {
		if (predicate()) return;
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.fail(message);
}

test('key_sync is page-decided even when the engine hello still lists it', () => {
	assert.equal(m.rustCommandUnsupported('key_sync'), false);
	assert.equal(m.rustCommandUnsupported('key_nudge'), true);
	assert.equal(m.PAGE_DECIDED.has('key_sync'), true);
	assert.equal(m.PAGE_DECIDED.has('key_shift'), true);
	assert.equal(m.FORWARDED.has('key_sync'), false);
});

test('key_shift reaches Rust as key_nudge and updates the acknowledged deck state', async () => {
	seed(1, { master_tempo_enabled: false });
	assert.equal(await m.executeInRustEngine({ type: 'key_shift', deck: 1, semitones: 2 }), true);
	assert.deepEqual(sent, [
		{ type: 'master_tempo', deck: 1, enabled: true },
		{ type: 'key_nudge', deck: 1, semitones: 2 }
	]);
	assert.equal(m.deckStates[1].key_shift_semitones, 2);
	assert.equal(
		sent.some((command) => command.type === 'key_shift' || command.type === 'key_sync'),
		false
	);
});

test('exact key_sync follows the target key without sending a key_sync wire command', async () => {
	seed(1, { key: '8A', is_master: true });
	seed(2, { key: '3A' });
	m.rustMaster.deck = 1;
	assert.equal(
		await m.executeInRustEngine({ type: 'key_sync', deck: 2, enabled: true, mode: 'exact' }),
		true
	);
	const expected = camelot.deriveKeySyncTargetManualShift('3A', '8A', 0, 0, 0, 'exact');
	assert.equal(m.deckStates[2].key_sync_enabled, true);
	assert.equal(m.deckStates[2].key_shift_semitones, expected);
	assert.ok(sent.some((command) => command.type === 'key_nudge'));
	assert.equal(
		sent.some((command) => command.type === 'key_sync'),
		false
	);
});

test('disabling key_sync restores the pre-arm shift', async () => {
	seed(1, { key: '8A', is_master: true });
	seed(2, { key: '3A', key_shift_semitones: 1 });
	m.rustMaster.deck = 1;
	await m.executeInRustEngine({ type: 'key_sync', deck: 2, enabled: true, mode: 'exact' });
	await m.executeInRustEngine({ type: 'key_sync', deck: 2, enabled: false });
	assert.equal(m.deckStates[2].key_shift_semitones, 1);
	assert.equal(m.deckStates[2].key_sync_enabled, false);
});

test('an absolute ladder pick disarms key_sync', async () => {
	seed(1, { key: '8A', is_master: true });
	seed(2, { key: '3A' });
	m.rustMaster.deck = 1;
	await m.executeInRustEngine({ type: 'key_sync', deck: 2, enabled: true, mode: 'exact' });
	await m.executeInRustEngine({ type: 'key_shift', deck: 2, semitones: 0 });
	assert.equal(m.deckStates[2].key_sync_enabled, false);
	assert.equal(m.deckStates[2].key_shift_semitones, 0);
});

test('key_sync rejects a self target and arms while waiting for a master', async () => {
	seed(1, { key: '8A', is_master: false });
	await assert.rejects(
		m.executeInRustEngine({ type: 'key_sync', deck: 1, enabled: true, target: 1 }),
		/cannot follow itself/
	);

	seed(2, { key: '3A' });
	await m.executeInRustEngine({ type: 'key_sync', deck: 2, enabled: true, mode: 'exact' });
	assert.equal(m.deckStates[2].key_sync_enabled, true);
	assert.equal(
		sent.some((command) => command.type === 'key_nudge'),
		false
	);
});

test('a zero-distance key shift does not send a nudge', async () => {
	seed(1, { key: '8A', master_tempo_enabled: true });
	await m.executeInRustEngine({ type: 'key_shift', deck: 1, semitones: 0 });
	assert.equal(
		sent.some((command) => command.type === 'key_nudge'),
		false
	);
});

test('native key sync serializes staggered follower acknowledgements without double nudges', async () => {
	seed(1, { key: '8A', is_master: true });
	seed(2, { key: '3A', key_sync_enabled: true });
	seed(3, { key: '3A', key_sync_enabled: true });
	m.rustMaster.deck = 1;
	const pending = [];
	let peakFollowerNudges = 0;
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt,
		send: (command) => {
			sent.push(command);
			if (command.type !== 'key_nudge') {
				return Promise.resolve({ type: 'result', id: null, ok: true });
			}
			return new Promise((resolve) => {
				pending.push({ command, resolve });
				peakFollowerNudges = Math.max(
					peakFollowerNudges,
					pending.filter(({ command: queued }) => queued.deck !== 1).length
				);
			});
		},
		positionMs: () => null
	});

	const leaderShift = m.executeInRustEngine({ type: 'key_shift', deck: 1, semitones: 2 });
	await waitFor(() => pending.length === 1, 'leader key nudge was not sent');
	assert.equal(pending[0].command.deck, 1);
	pending.shift().resolve({ type: 'result', id: null, ok: true });
	await leaderShift;

	const follow = m.flushRustKeySyncFollowForTest();
	await waitFor(() => pending.length === 1, 'first follower key nudge was not sent');
	assert.equal(pending[0].command.deck, 2);
	pending.shift().resolve({ type: 'result', id: null, ok: true });
	await waitFor(() => pending.length === 1, 'second follower key nudge was not sent');
	assert.equal(pending[0].command.deck, 3);
	pending.shift().resolve({ type: 'result', id: null, ok: true });
	await follow;

	const expected = camelot.deriveKeySyncTargetManualShift('3A', '8A', 0, 2, 0, 'exact');
	for (const deck of [2, 3]) {
		const deltas = sent
			.filter((command) => command.type === 'key_nudge' && command.deck === deck)
			.map((command) => command.semitones);
		assert.deepEqual(deltas, [expected]);
		assert.equal(
			deltas.reduce((sum, delta) => sum + delta, 0),
			expected
		);
		assert.equal(m.deckStates[deck].key_shift_semitones, expected);
	}
	assert.equal(peakFollowerNudges, 1);
	assert.equal(pending.length, 0);
	assert.equal(sent.filter((command) => command.type === 'key_nudge').length, 3);
});

test('native key sync follow runner claims every deck and sync behind prior work', async () => {
	const scheduler = new ScopedCommandScheduler();
	const restore = m.installKeySyncFollowRunner((work) => scheduler.run([1, 2, 3, 4, 'sync'], work));
	const events = [];
	let release;
	const prior = scheduler.run(['sync'], async () => {
		events.push('prior');
		await new Promise((resolve) => {
			release = resolve;
		});
	});
	try {
		const follow = m.runKeySyncFollow(async () => {
			events.push('follow');
		});
		const later = scheduler.run([2], async () => {
			events.push('later');
		});
		await Promise.resolve();
		await Promise.resolve();
		assert.deepEqual(events, ['prior']);
		release();
		await Promise.all([prior, follow, later]);
		assert.deepEqual(events, ['prior', 'follow', 'later']);
	} finally {
		restore();
	}
});
