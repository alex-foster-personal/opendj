import assert from 'node:assert/strict';
import { afterEach, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * The perf ring is the ONLY durable record of a deck load, and it used to be a
 * single 40-slot FIFO shared by every kind of row.
 *
 * The defect that motivates this file: PitchFader fires an unthrottled
 * pointermove, each one reaches _scheduleDeckSerial, and each schedule appends a
 * `transport-schedule` row. One ~40-sample fader drag therefore evicted every
 * `deck-load`, `deck-load-fail` and `audio-context` row in the ring, so
 * `window.__mdtLastLoads()` came back empty exactly when someone was mid-session
 * and wanted to know why the last load was slow. Nothing crashed; the KPI just
 * silently reported nothing, which is the failure mode no other test catches.
 *
 * Second defect: every append did a full JSON.stringify of the ring plus a
 * synchronous localStorage.setItem, on the main thread, during that drag, while
 * audio was playing.
 *
 * Regression lines:
 * - if one noisy kind can evict another kind's rows then a fader drag wipes the
 *   load KPI and __mdtLastLoads() returns nothing useful
 * - if the per-kind budgets stop being enforced then the ring grows without
 *   bound and the localStorage write grows with it
 * - if eviction stops being oldest-first WITHIN a kind then the ring keeps stale
 *   rows and drops the ones a live session cares about
 * - if the retained rows stop being chronological then the e2e latency floor,
 *   which reads __mdtPerfLog() and treats it as newest-last, reads backwards
 * - if every write goes straight to localStorage again then the stringify+setItem
 *   is back on the pointermove path this change exists to clear
 * - if the debounced flush never lands then the ring stops surviving a reload
 * - if nothing flushes on pagehide then the last burst before a navigation is lost
 */

const STORAGE_KEY = 'mdt.perfEventLog';
const FLUSH_DEBOUNCE_MS = 250;

/** Budgets under test. Mirrored here on purpose: if the module changes them,
 * this file must be re-read by a human rather than silently follow along. */
const DECK_LOAD_BUDGET = 16;
const TRANSPORT_SCHEDULE_BUDGET = 16;
const DECK_STATE_BUDGET = 8;
const OTHER_BUDGET = 8;
const CAPACITY =
	DECK_LOAD_BUDGET + TRANSPORT_SCHEDULE_BUDGET + DECK_STATE_BUDGET + OTHER_BUDGET;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

/** localStorage that counts its writes, so "one write per burst" is assertable. */
function makeLocalStorage(seed = {}) {
	const map = new Map(Object.entries(seed));
	const store = {
		writes: 0,
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => {
			store.writes += 1;
			map.set(key, String(value));
		},
		removeItem: (key) => {
			map.delete(key);
		}
	};
	return store;
}

/** A window whose listeners can be fired back at the module under test. */
function makeWindow() {
	const listeners = new Map();
	return {
		listeners,
		addEventListener: (type, handler) => {
			const forType = listeners.get(type) ?? [];
			forType.push(handler);
			listeners.set(type, forType);
		},
		fire: (type) => {
			for (const handler of listeners.get(type) ?? []) handler();
		},
		countFor: (type) => (listeners.get(type) ?? []).length
	};
}

/**
 * The instance handed out by the previous freshLog, so its pending flush can be
 * drained before the next one starts. See freshLog.
 */
let _priorLog = null;

/**
 * A fresh module instance per test. The ring is module state, so reusing one
 * instance would let an earlier test's 60 rows decide a later test's budget.
 *
 * The previous instance is DRAINED first, and that is load-bearing rather than
 * tidiness. Whichever test ran last may have left a real FLUSH_DEBOUNCE_MS
 * timer armed, and that timer resolves `localStorage` when it FIRES, not when
 * it was armed - so it writes the previous test's ring into this test's store,
 * and it gets its chance during the `await` below. Seen live: the 40-row
 * capacity test's flush landed inside the legacy-ring test's module load, which
 * then read 40 rows that no test had seeded and reported the trim as broken.
 * Flushing first clears the outstanding rows, so that pending timer fires as
 * the no-op flushPerfEventLog already documents.
 */
async function freshLog({ seed, withWindow = false } = {}) {
	_priorLog?.flushPerfEventLog();
	const store = makeLocalStorage(seed);
	defineGlobal('localStorage', store);
	const win = withWindow ? makeWindow() : undefined;
	if (withWindow) defineGlobal('window', win);
	else delete globalThis.window;
	const perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	_priorLog = perfLog;
	store.writes = 0; // module init reads; it must not write
	return { perfLog, store, win };
}

function kindsOf(rows) {
	return rows.map((row) => row.kind);
}

function countKind(rows, prefix) {
	return rows.filter((row) => row.kind.startsWith(prefix)).length;
}

afterEach(() => {
	mock.timers.reset();
	delete globalThis.window;
});

//-----------------------------------------------------------------------------
// defect 1: one kind must not be able to evict another
//-----------------------------------------------------------------------------

test('a fader drag worth of transport rows cannot evict the deck-load rows', async () => {
	const { perfLog } = await freshLog();

	perfLog.recordPerfTiming('deck-load sid=aaaaaaaaaaaa', { total: 1200 }, 1);
	perfLog.recordPerfEvent('deck-load-fail', 'AUDIO_FILE_MISSING', 2);
	perfLog.recordPerfTiming('audio-context', { sample_rate_hz: 48000 }, null);

	// The drag: 60 unthrottled pointermove schedules, more than the old MAX of 40.
	for (let i = 0; i < 60; i += 1) {
		perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: 8 + i }, 1);
	}

	const rows = perfLog.readPerfEvents();
	assert.equal(
		countKind(rows, 'deck-load'),
		2,
		'if a drag evicts the load rows then __mdtLastLoads() is empty exactly when ' +
			'someone is mid-session asking why the last load was slow'
	);
	assert.equal(
		countKind(rows, 'audio-context'),
		1,
		'the device-floor stamp is written once per context; losing it makes every ' +
			'scheduled_offset_ms in the ring incomparable across machines'
	);
	assert.equal(
		perfLog.lastDeckLoadEvents(4).length,
		2,
		'the load KPI must still read back after the drag'
	);
	assert.equal(countKind(rows, 'transport-schedule'), TRANSPORT_SCHEDULE_BUDGET);
	assert.ok(rows.length <= CAPACITY, `ring must stay bounded, got ${rows.length}`);
});

test('each kind evicts oldest-first inside its own budget', async () => {
	const { perfLog } = await freshLog();

	for (let i = 0; i < 20; i += 1) {
		perfLog.recordPerfTiming(`deck-load sid=track${i}`, { total: i }, 1);
	}

	const loads = perfLog.readPerfEvents().filter((row) => row.kind.startsWith('deck-load'));
	assert.equal(loads.length, DECK_LOAD_BUDGET);
	assert.equal(
		loads[0].stages.total,
		4,
		'if eviction is not oldest-first then the ring keeps stale loads and drops the ' +
			'ones the current session cares about'
	);
	assert.equal(loads[loads.length - 1].stages.total, 19);
});

test('the low-volume kinds share one budget and cannot starve each other out', async () => {
	const { perfLog } = await freshLog();

	perfLog.recordPerfTiming('deck-load sid=keepme', { total: 900 }, 3);
	for (let i = 0; i < 20; i += 1) {
		perfLog.recordPerfEvent('sync-failure', `could not phase lock ${i}`, 2);
	}

	const rows = perfLog.readPerfEvents();
	assert.equal(countKind(rows, 'sync-failure'), OTHER_BUDGET);
	assert.equal(
		countKind(rows, 'deck-load'),
		1,
		'a burst of unrelated rows must not reach into the deck-load budget'
	);
});

test('deck-state rows have their own budget and cannot be evicted by the quiet kinds', async () => {
	const { perfLog } = await freshLog();

	// Legacy empty-deck boot rows plus real unload rows are the ring rows the
	// resource probe reads deck state from. Before they had their own budget
	// they shared the 8-row `other` remainder with every quiet kind, so an
	// audio-context or sync-failure flood evicted them and the probe read
	// loaded_deck_count None under ordinary use (issue #1403).
	for (const deck of [1, 2, 3, 4]) {
		perfLog.recordPerfEvent('deck-state-empty', 'initial deck state is unloaded', deck, 'info');
	}
	perfLog.recordPerfEvent('deck-unload', 'deck resources released', 1, 'info');
	perfLog.recordPerfEvent('deck-unload', 'deck resources released', 2, 'info');
	for (let i = 0; i < 30; i += 1) {
		perfLog.recordPerfEvent('sync-failure', `could not phase lock ${i}`, 2);
	}

	const rows = perfLog.readPerfEvents();
	assert.equal(
		countKind(rows, 'deck-state-empty'),
		4,
		'the empty-deck boot rows must survive a flood of unrelated quiet rows'
	);
	assert.equal(countKind(rows, 'deck-unload'), 2, 'unload rows must survive the same flood');
	assert.equal(
		countKind(rows, 'sync-failure'),
		OTHER_BUDGET,
		'the flood is capped inside its own budget, not spread into deck-state'
	);
});

test('the empty-deck baseline is a non-ring record, not four ring rows', async () => {
	const { perfLog, store } = await freshLog();

	perfLog.recordDeckStateBaseline();

	assert.equal(
		perfLog.readPerfEvents().length,
		0,
		'recording the constant empty-deck state must not spend ring budget'
	);
	const stamp = JSON.parse(store.getItem('mdt.deckState'));
	assert.equal(stamp.v, 1);
	assert.deepEqual(stamp.unloaded, [1, 2, 3, 4]);
	assert.ok(
		typeof stamp.t === 'string' && stamp.t.length > 0,
		'the baseline carries the instant the decks were created empty'
	);
});

test('the whole ring stays bounded no matter which kind is noisy', async () => {
	const { perfLog } = await freshLog();

	for (let i = 0; i < 200; i += 1) {
		perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: i }, 1);
		perfLog.recordPerfTiming(`deck-load sid=t${i}`, { total: i }, 1);
		perfLog.recordPerfEvent('deck-unload', 'deck resources released', 2, 'info');
		perfLog.recordPerfEvent('beat-sync-skip', `skip ${i}`, 2);
	}

	assert.equal(
		perfLog.readPerfEvents().length,
		CAPACITY,
		'if the total is unbounded then the flushed JSON grows without limit'
	);
});

test('retained rows stay chronological, which is the contract __mdtPerfLog exposes', async () => {
	const { perfLog } = await freshLog();

	for (let i = 0; i < 12; i += 1) {
		perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: i }, 1);
		perfLog.recordPerfTiming(`deck-load sid=t${i}`, { total: i }, 2);
	}

	const rows = perfLog.readPerfEvents();
	const offsets = rows
		.filter((row) => row.kind === 'transport-schedule')
		.map((row) => row.stages.scheduled_offset_ms);
	assert.deepEqual(
		offsets,
		[...offsets].sort((a, b) => a - b),
		'the e2e latency floor reads __mdtPerfLog() as newest-LAST; re-ordering it ' +
			'silently inverts every offset comparison built on it'
	);
	// Interleaving must survive too: the two kinds are not segregated into blocks.
	assert.ok(
		kindsOf(rows).indexOf('transport-schedule') < kindsOf(rows).lastIndexOf('transport-schedule'),
		'sanity: both kinds are present in one chronological sequence'
	);
});

test('a legacy ring saved before the budgets existed is trimmed on read', async () => {
	// Everyone already has a 40-row blob on disk, and after a fader drag that blob
	// is 40 transport-schedule rows. Reading it back unfiltered would reinstate
	// the exact over-budget state this change removes.
	const legacy = Array.from({ length: 40 }, (_, i) => ({
		t: new Date(1700000000000 + i).toISOString(),
		kind: 'transport-schedule',
		deck: 1,
		message: `scheduled_offset_ms=${i}`,
		stages: { scheduled_offset_ms: i }
	}));
	const { perfLog } = await freshLog({ seed: { [STORAGE_KEY]: JSON.stringify(legacy) } });

	const rows = perfLog.readPerfEvents();
	assert.equal(rows.length, TRANSPORT_SCHEDULE_BUDGET);
	assert.equal(
		rows[rows.length - 1].stages.scheduled_offset_ms,
		39,
		'trimming a stored ring must keep the NEWEST rows'
	);
});

//-----------------------------------------------------------------------------
// defect 2: the write must leave the main-thread gesture path
//-----------------------------------------------------------------------------

test('a burst of writes costs one localStorage write, not one per row', async () => {
	const { perfLog, store } = await freshLog();
	mock.timers.enable({ apis: ['setTimeout'] });

	for (let i = 0; i < 40; i += 1) {
		perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: i }, 1);
	}
	assert.equal(
		store.writes,
		0,
		'if a row is stringified and persisted synchronously then the write is back on ' +
			'the pointermove path, during playback, which is the defect'
	);

	mock.timers.tick(FLUSH_DEBOUNCE_MS);
	assert.equal(store.writes, 1, 'the whole burst must coalesce into a single flush');
	assert.deepEqual(
		JSON.parse(store.getItem(STORAGE_KEY)),
		JSON.parse(JSON.stringify(perfLog.readPerfEvents())),
		'the flushed blob must be the in-memory ring, or a reload reads a different log'
	);
});

test('the in-memory ring is authoritative before the flush lands', async () => {
	const { perfLog, store } = await freshLog();
	mock.timers.enable({ apis: ['setTimeout'] });

	perfLog.recordPerfTiming('deck-load sid=unflushed', { total: 42 }, 1);
	assert.equal(store.getItem(STORAGE_KEY), null, 'nothing is persisted yet');
	assert.equal(
		perfLog.lastDeckLoadEvents(1)[0].stages.total,
		42,
		'if readers had to wait for the flush then __mdtLastLoads() would lag the ' +
			'load it is being asked about'
	);
});

test('a later burst schedules its own flush rather than going silent', async () => {
	const { perfLog, store } = await freshLog();
	mock.timers.enable({ apis: ['setTimeout'] });

	perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: 1 }, 1);
	mock.timers.tick(FLUSH_DEBOUNCE_MS);
	assert.equal(store.writes, 1);

	perfLog.recordPerfTiming(`deck-load sid=second`, { total: 7 }, 2);
	mock.timers.tick(FLUSH_DEBOUNCE_MS);
	assert.equal(store.writes, 2, 'if the flush only ever arms once then the ring stops persisting');
	assert.ok(
		store.getItem(STORAGE_KEY).includes('deck-load sid=second'),
		'the second flush must carry the rows written after the first one'
	);
});

test('pagehide flushes the rows written since the last timer, exactly once', async () => {
	const { perfLog, store, win } = await freshLog({ withWindow: true });
	mock.timers.enable({ apis: ['setTimeout'] });

	perfLog.recordPerfTiming('deck-load sid=lastbeforenav', { total: 88 }, 4);
	perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: 8 }, 4);
	assert.equal(store.writes, 0, 'still debounced');

	assert.equal(
		win.countFor('pagehide'),
		1,
		'if the listener is not installed then a navigation drops the last burst'
	);
	win.fire('pagehide');
	assert.equal(store.writes, 1);
	assert.ok(store.getItem(STORAGE_KEY).includes('deck-load sid=lastbeforenav'));

	// The pending timer must not then write a second time for the same rows.
	mock.timers.tick(FLUSH_DEBOUNCE_MS);
	assert.equal(
		store.writes,
		1,
		'a flush already taken by pagehide must disarm the timer, not double-write'
	);
});

test('the pagehide listener is installed once, not once per recorded row', async () => {
	const { perfLog, win } = await freshLog({ withWindow: true });
	mock.timers.enable({ apis: ['setTimeout'] });

	for (let i = 0; i < 30; i += 1) {
		perfLog.recordPerfTiming('transport-schedule', { scheduled_offset_ms: i }, 1);
	}
	assert.equal(
		win.countFor('pagehide'),
		1,
		'if a listener is added per row then a drag leaks 40 handlers per gesture'
	);
});

test('flushPerfEventLog persists on demand for a caller that cannot wait', async () => {
	const { perfLog, store } = await freshLog();
	mock.timers.enable({ apis: ['setTimeout'] });

	perfLog.recordPerfEvent('deck-load-fail', 'AUDIO_FILE_MISSING', 1);
	perfLog.flushPerfEventLog();
	assert.equal(store.writes, 1);
	assert.ok(store.getItem(STORAGE_KEY).includes('AUDIO_FILE_MISSING'));
});

test('resetPerfEventLog empties the ring and persists the empty store', async () => {
	const { perfLog, store } = await freshLog();
	mock.timers.enable({ apis: ['setTimeout'] });

	perfLog.recordPerfEvent('deck-load-fail', 'before-reset', 1);
	perfLog.flushPerfEventLog();
	assert.ok(store.getItem(STORAGE_KEY).includes('before-reset'));

	perfLog.resetPerfEventLog();
	assert.equal(perfLog.readPerfEvents().length, 0);
	const stored = JSON.parse(store.getItem(STORAGE_KEY));
	assert.equal(stored.length, 0, 'reset must flush an empty ring to localStorage');
});
