import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The deck error banner never fades and has its own dismiss control, so it can
// stand on a deck for an entire set. Before this module it rendered a bare
// error string and minted no id, so the words on screen tied to no row in any
// log and two identical failures an hour apart were indistinguishable.
//
// Everything below is asserted by EXECUTION - calling the module and reading
// what it returned and what it logged. Nothing here greps the source for a
// call site, because a source-string assertion passes for code that is present
// but wrong, which is the failure mode these tests exist to catch.

/** Intercept the shared global `console` the bundled code looks up at call
 * time, the same way toast-perf-log.test.mjs does. */
function _captureConsole() {
	const calls = { info: [], warn: [], error: [] };
	const real = { info: console.info, warn: console.warn, error: console.error };
	console.info = (...args) => calls.info.push(args.join(' '));
	console.warn = (...args) => calls.warn.push(args.join(' '));
	console.error = (...args) => calls.error.push(args.join(' '));
	return {
		calls,
		restore: () => {
			console.info = real.info;
			console.warn = real.warn;
			console.error = real.error;
		}
	};
}

async function _load() {
	return await loadTypeScriptModule('src/lib/rb/deck-error-id.svelte.ts');
}

test('a banner error is given an id, and the SAME id is what the log line carries', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(2, 'Beat Sync partial failure: succeeded [1], failed [2]');
	} finally {
		capture.restore();
	}

	const id = mod.deckErrorIds[2];
	assert.notEqual(id, null, 'the banner error was given no id, so it is unfindable in any log');

	assert.equal(
		capture.calls.error.length,
		1,
		'raising a banner error wrote no durable log row, so the id on screen leads nowhere'
	);
	const line = capture.calls.error[0];
	assert.ok(
		line.includes(id),
		`the log line does not carry the id the banner shows (id=${id}, line=${line})`
	);
	assert.ok(
		line.includes('Beat Sync partial failure'),
		'the log line carries the id but not the failure it identifies'
	);
	assert.ok(
		line.includes('deck=2'),
		'the log line does not say which deck raised it'
	);
});

test('the id is greppable, not a bare integer that matches every digit in a log', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(1, 'set deck master failed');
	} finally {
		capture.restore();
	}
	const id = mod.deckErrorIds[1];
	assert.match(
		id,
		/^t-\d+$/,
		`the id "${id}" is not a prefixed token, so searching a log for it matches unrelated text`
	);
});

test('one raising keeps one id: re-noting the same message neither re-mints nor re-logs', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		// Deck.svelte drives this from an $effect over a $derived that
		// re-evaluates on unrelated deck state, so this happens constantly
		// while a banner is standing.
		mod.noteDeckError(3, 'processor failed - worklet gone');
		const first = mod.deckErrorIds[3];
		mod.noteDeckError(3, 'processor failed - worklet gone');
		mod.noteDeckError(3, 'processor failed - worklet gone');
		assert.equal(
			mod.deckErrorIds[3],
			first,
			'the id changed under the reader while the same banner was showing'
		);
	} finally {
		capture.restore();
	}
	assert.equal(
		capture.calls.error.length,
		1,
		`one standing banner wrote ${capture.calls.error.length} log rows, so a re-render floods the ring`
	);
});

test('a different message on the same deck is a different incident with a different id', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(1, 'first failure');
		const first = mod.deckErrorIds[1];
		mod.noteDeckError(1, 'second, unrelated failure');
		const second = mod.deckErrorIds[1];
		assert.notEqual(
			second,
			first,
			'a new failure inherited the previous failure id, so the log points at the wrong incident'
		);
	} finally {
		capture.restore();
	}
	assert.equal(capture.calls.error.length, 2, 'the second failure was not logged');
});

test('the same message raised again after a dismissal is a NEW incident, not the old one', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(4, 'set deck master failed');
		const first = mod.deckErrorIds[4];
		// This is what dismissPerformanceDeckError does: the condition is
		// unchanged and re-reports if it recurs.
		mod.noteDeckError(4, null);
		mod.noteDeckError(4, 'set deck master failed');
		assert.notEqual(
			mod.deckErrorIds[4],
			first,
			'a second raising reused the first raising id, so two incidents collapse into one in the log'
		);
	} finally {
		capture.restore();
	}
	assert.equal(capture.calls.error.length, 2, 'the second raising was not logged as its own row');
});

test('dismissing clears the id, so no stale id outlives its banner', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(2, 'transient failure');
		assert.notEqual(mod.deckErrorIds[2], null);
		mod.noteDeckError(2, null);
	} finally {
		capture.restore();
	}
	assert.equal(
		mod.deckErrorIds[2],
		null,
		'a dismissed banner left its id behind, so the UI can render an id with no error'
	);
});

test('decks do not share or evict each other ids', async () => {
	const mod = await _load();
	const capture = _captureConsole();
	try {
		mod.noteDeckError(1, 'deck one failure');
		const one = mod.deckErrorIds[1];
		mod.noteDeckError(2, 'deck two failure');
		assert.equal(
			mod.deckErrorIds[1],
			one,
			'raising an error on deck 2 changed deck 1 id'
		);
		assert.notEqual(
			mod.deckErrorIds[2],
			one,
			'two decks are showing the same id, so a search cannot tell them apart'
		);
		mod.noteDeckError(1, null);
		assert.notEqual(
			mod.deckErrorIds[2],
			null,
			'dismissing deck 1 cleared deck 2 banner id'
		);
	} finally {
		capture.restore();
	}
});

// ----- the shared counter -------------------------------------------------

test('ids come from one strictly increasing counter, so no two raisings collide', async () => {
	const ids = await loadTypeScriptModule('src/lib/rb/error-id.ts');
	const seen = new Set();
	let previous = 0;
	for (let i = 0; i < 200; i++) {
		const next = ids.mintErrorId();
		assert.ok(next > previous, `the counter went backwards: ${next} after ${previous}`);
		assert.ok(!seen.has(next), `the counter repeated ${next}, so two incidents share an id`);
		seen.add(next);
		previous = next;
	}
});

test('formatErrorId refuses a sequence it cannot make a findable id from', async () => {
	const ids = await loadTypeScriptModule('src/lib/rb/error-id.ts');
	assert.equal(ids.formatErrorId(7), 't-7');
	for (const bad of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.throws(
			() => ids.formatErrorId(bad),
			RangeError,
			`formatErrorId(${bad}) produced an id instead of failing fast`
		);
	}
});
