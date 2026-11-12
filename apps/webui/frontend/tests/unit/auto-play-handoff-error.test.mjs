/**
 * Issue #12381: a refused handoff must not strand its reservation or show false success.
 *
 * [if] AutoPlay reserves a candidate for a handoff [then]
 * [if] the load or schedule command rejects [then]
 * [if] AutoPlay evaluates the next decision [then] no stuck reservation and no false success.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

const POLL_SETTLE_MS = 900;
const DURATION_MS = 100_000;
const HANDOFF_POSITION_MS = 95_000;

const ENTRY = readFileSync(
	fileURLToPath(new URL('./fixtures/autoplay-handoff-error-entry.ts', import.meta.url)),
	'utf8'
)
	.replace(/^\/\*[\s\S]*?\*\/\s*/m, '')
	.replace(/^\/\/.*\n/gm, '');

const settle = () => new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));

function installBrowserShim() {
	const saved = new Map();
	const set = (key, value) => {
		saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
		Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
	};
	set('window', globalThis);
	set('location', { href: 'http://127.0.0.1:0/performance', pathname: '/performance' });
	set('navigator', { userAgent: 'auto-play-handoff-error-test' });
	set('isSecureContext', true);
	return () => {
		for (const [key, descriptor] of saved) {
			if (descriptor === undefined) delete globalThis[key];
			else Object.defineProperty(globalThis, key, descriptor);
		}
	};
}

const feedRow = (stable_id, key = '8A', bpm = 124) => ({
	stable_id,
	key,
	bpm,
	file_exists: true
});

function installFakeTransport(entry) {
	const log = [];
	const loadGates = new Map();
	const unloadGates = new Map();
	const playGates = new Map();

	entry.engine.load = async (deckId, stableId) => {
		log.push(`load ${stableId}`);
		const gate = loadGates.get(stableId);
		if (gate === 'reject') throw new Error('decode failed');
		const target = entry.deckStates[deckId];
		target.stable_id = stableId;
		target.duration_ms = DURATION_MS;
		target.position_ms = 0;
		target.playing = false;
		target.key = '8A';
		target.bpm = 124;
	};
	const unloadRejectCounts = new Map();
	entry.engine.unload = async (deckId) => {
		const sid = entry.deckStates[deckId].stable_id;
		log.push(`unload ${sid ?? 'empty'}`);
		const gate = unloadGates.get(deckId);
		if (gate !== undefined) {
			const seen = (unloadRejectCounts.get(deckId) ?? 0) + 1;
			unloadRejectCounts.set(deckId, seen);
			const limit =
				gate === 'reject' ? Number.POSITIVE_INFINITY : typeof gate === 'number' ? gate : 1;
			if (seen <= limit) throw new Error('engine busy');
		}
		const target = entry.deckStates[deckId];
		target.stable_id = null;
		target.duration_ms = null;
		target.position_ms = 0;
		target.playing = false;
	};
	entry.engine.play = async (deckId) => {
		const sid = entry.deckStates[deckId].stable_id;
		log.push(`play ${sid ?? 'empty'}`);
		if (playGates.get(deckId) === 'reject') throw new Error('play refused');
		entry.deckStates[deckId].playing = true;
	};

	return { log, loadGates, unloadGates, playGates };
}

function armMaster(entry, { positionMs = HANDOFF_POSITION_MS } = {}) {
	Object.assign(entry.deckStates[1], {
		stable_id: 'src-1',
		playing: true,
		audible: true,
		is_master: true,
		duration_ms: DURATION_MS,
		position_ms: positionMs,
		key: '8A',
		bpm: 124,
		beat_sync_enabled: false
	});
	Object.assign(entry.deckStates[2], {
		stable_id: null,
		playing: false,
		audible: false,
		is_master: false,
		duration_ms: null,
		position_ms: 0,
		key: null,
		bpm: null,
		beat_sync_enabled: false
	});
}

async function withLiveController(run) {
	const probe = installTimerProbe();
	const restoreBrowser = installBrowserShim();
	const realFetch = globalThis.fetch;
	globalThis.fetch = async () =>
		new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	let uninstallAutoPlay = null;
	let uninstallIpc = null;
	try {
		const entry = await loadRuneModule(ENTRY);
		entry.uiPrefs.auto_play_enabled = true;
		uninstallIpc = entry.installPerformanceBrowserIpc();
		uninstallAutoPlay = entry.installAutoPlay();
		await probe.flush();
		await run(entry);
	} finally {
		if (uninstallAutoPlay !== null) uninstallAutoPlay();
		if (uninstallIpc !== null) uninstallIpc();
		globalThis.fetch = realFetch;
		restoreBrowser();
		probe.restore();
	}
}

describe('AutoPlay handoff refusal restores reservation (#12381)', { concurrency: false }, () => {
	it('[load phase] quarantines the failed id and hands off the next compatible track', async () => {
		await withLiveController(async (entry) => {
			const { log, loadGates } = installFakeTransport(entry);
			entry.setAutoPlayTrackFeed('handoff-load-reject', [
				feedRow('src-1'),
				feedRow('bad-1'),
				feedRow('good-1')
			]);
			loadGates.set('bad-1', 'reject');
			armMaster(entry);

			await settle();
			await settle();

			assert.equal(entry.readAutoPlayHandoffInFlight(), false, 'handoff must not stay in flight');
			if (entry.readAutoPlayHandoffPending()) {
				assert.equal(
					entry.deckStates[2].stable_id,
					'good-1',
					'handoff_pending must not outlive a rejected load without a real follower track'
				);
			}
			assert.notEqual(
				entry.deckStates[2].stable_id,
				'bad-1',
				'failed candidate must not land on follower'
			);
			assert.equal(
				entry.deckStates[2].stable_id,
				'good-1',
				'the next decision must load the surviving candidate'
			);
			assert.ok(
				log.includes('load bad-1') && log.includes('load good-1'),
				`expected load attempts for bad-1 then good-1, log=${log.join(' | ')}`
			);
			assert.equal(log.indexOf('play bad-1'), -1, 'must not treat a rejected load as success');
		});
	});

	it('[follower-unload phase] clears the claim so the same candidate can retry', async () => {
		await withLiveController(async (entry) => {
			const { log, unloadGates } = installFakeTransport(entry);
			entry.setAutoPlayTrackFeed('handoff-unload-reject', [feedRow('src-1'), feedRow('next-1')]);
			armMaster(entry);
			Object.assign(entry.deckStates[2], {
				stable_id: 'old-1',
				playing: false,
				audible: false,
				duration_ms: DURATION_MS,
				position_ms: 0,
				key: '8A',
				bpm: 124
			});
			unloadGates.set(2, 1);

			await settle();
			await settle();
			await settle();

			assert.equal(entry.readAutoPlayHandoffInFlight(), false);
			assert.equal(
				entry.deckStates[2].stable_id,
				'next-1',
				'retry after unload refusal must still reach load'
			);
			const loadNext = log.filter((line) => line === 'load next-1').length;
			assert.ok(loadNext >= 1, `expected a load of next-1 after retry, log=${log.join(' | ')}`);
			assert.ok(
				log.includes('unload old-1'),
				'follower-unload refusal must have attempted to clear the deck'
			);
		});
	});

	it('[commit phase] pins after load and does not load a second candidate on the same source', async () => {
		await withLiveController(async (entry) => {
			const { log, playGates } = installFakeTransport(entry);
			entry.setAutoPlayTrackFeed('handoff-commit-reject', [
				feedRow('src-1'),
				feedRow('first-1'),
				feedRow('second-1')
			]);
			playGates.set(2, 'reject');
			armMaster(entry);

			await settle();
			await settle();

			assert.equal(entry.readAutoPlayHandoffInFlight(), false);
			assert.equal(entry.deckStates[2].stable_id, 'first-1', 'load landed before play refused');
			assert.ok(log.includes('load first-1'), `log=${log.join(' | ')}`);
			assert.ok(log.includes('play first-1'));
			assert.equal(
				log.filter((line) => line.startsWith('load ')).length,
				1,
				'commit failure must not trigger a second handoff load'
			);
			assert.ok(
				!log.some((line) => line === 'load second-1'),
				'must not pick another candidate after a committed load failure'
			);
		});
	});
});
