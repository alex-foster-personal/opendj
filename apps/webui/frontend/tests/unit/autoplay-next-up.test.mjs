/**
 * Lost-response retry for the ahead-analysis next-up PUT (NATIVE-5886).
 *
 * The chart the page wants and the chain the server was last known to hold
 * are different facts. A PUT that fails after the server applied it must
 * forget that assumed chain, or a later desired chain equal to the old one
 * looks confirmed and the retry is skipped.
 *
 * [if] A is accepted, B's response is lost, and the page wants A again
 *   [then] the next PUT is A [else the server keeps B].
 * [if] B's response is lost and the page still wants B
 *   [then] the retry is B [else a failure never repeats the desired chain].
 * [if] the page clears before any engine boot is known
 *   [then] no PUT yet [else a clear is aimed at an engine nobody has seen].
 * [if] the engine boot becomes known, with or without a chain
 *   [then] the page's chain is PUT once, empty included
 *   [else a reloaded page leaves the previous page's tracks eligible].
 * [if] a new boot is seen while a PUT is in flight
 *   [then] that PUT's completion confirms nothing and the chain is sent again
 *   [else the restarted engine never hears the chain].
 * [if] a sent chain is cleared
 *   [then] the next PUT is empty [else the drain keeps analyzing the old chain].
 * [if] that empty clear's response is lost
 *   [then] the retry is empty [else a failed clear never repeats].
 */
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { startedAsCli } from '../live/arm-autoplay-next-up.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const NEXT_UP_PATH = '/api/v1/ahead-analysis/next-up';

test('importing the live arm does not run its stdin CLI', () => {
	assert.equal(typeof startedAsCli, 'function');
	assert.equal(startedAsCli(), false);
});

test('an entry point that cannot be resolved throws, it does not read as an import', () => {
	const entry = process.argv[1];
	try {
		process.argv[1] = '/nonexistent-arm-autoplay-next-up/entry.mjs';
		assert.throws(() => startedAsCli(), { code: 'ENOENT' });
		process.argv[1] = undefined;
		assert.equal(startedAsCli(), false, 'no entry at all is the import case');
	} finally {
		process.argv[1] = entry;
	}
});

function jsonOk(stableIds) {
	return new Response(JSON.stringify({ ok: true, next_up: stableIds }), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

async function stableIdsOf(input, init) {
	const method = input instanceof Request ? input.method : init?.method ?? 'GET';
	const url = input instanceof Request ? input.url : String(input);
	if (method !== 'PUT' || !url.includes(NEXT_UP_PATH)) {
		throw new Error(`unexpected request ${method} ${url}`);
	}
	const text = input instanceof Request ? await input.text() : String(init?.body ?? '');
	const body = JSON.parse(text);
	if (!Array.isArray(body.stable_ids)) throw new Error('PUT body has no stable_ids');
	return body.stable_ids;
}

/** Record every next-up PUT. `decide` returns the response for that call. */
function installFetch(decide) {
	const calls = [];
	globalThis.fetch = async (input, init) => {
		const ids = await stableIdsOf(input, init);
		calls.push(ids);
		return decide(ids, calls.length);
	};
	return calls;
}

async function until(label, predicate, ms = 1000) {
	const deadline = Date.now() + ms;
	while (!predicate()) {
		if (Date.now() > deadline) throw new Error(label);
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
}

/** A tab leadership stand-in; `leads` flips it and notifies like the real one. */
function fakeLease(initial) {
	let leading = initial;
	const listeners = new Set();
	return {
		isLeader: () => leading,
		subscribe(listener) {
			listeners.add(listener);
			return () => listeners.delete(listener);
		},
		leads(next) {
			leading = next;
			for (const listener of [...listeners]) listener();
		}
	};
}

async function loadNextUp({ lease = fakeLease(true) } = {}) {
	// Node's fetch rejects a path-only URL. The browser uses same-origin;
	// the test points the client at a loopback base and answers it itself.
	const mod = await loadTypeScriptModule('src/lib/rb/autoplay-next-up.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
	// The sole-leader tab every earlier test describes: its behavior is unchanged.
	if (lease !== null) mod.bindNextUpLeadership(lease, 'tab-a');
	return mod;
}

describe('next-up lost response', { concurrency: 1 }, () => {
	test('a lost B response retries A when the page has returned to A', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		let rejectB = () => {};
		const lost = new Promise((_resolve, reject) => {
			rejectB = reject;
		});
		const calls = installFetch((_ids, n) => (n === 2 ? lost : jsonOk(_ids)));
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await until('accepted A never left the page', () => calls.length === 1);
			await new Promise((resolve) => setTimeout(resolve, 20));
			mod.publishAutoPlayNextUp(['track-b']);
			await until('B never went in flight', () => calls.length === 2);
			mod.publishAutoPlayNextUp(['track-a']);
			rejectB(new TypeError('response lost'));
			await until('lost B did not retry the desired A chain', () => calls.length >= 3);
			assert.deepEqual(calls[0], ['track-a']);
			assert.deepEqual(calls[1], ['track-b']);
			assert.deepEqual(calls[2], ['track-a']);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('an empty chain waits for the engine boot, then clears what a previous page left', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.publishAutoPlayNextUp([]);
			await new Promise((resolve) => setTimeout(resolve, 80));
			assert.equal(calls.length, 0, 'no engine has been seen yet, so there is nothing to clear');
			mod.noteEngineBoot('boot-a');
			await until('the reloaded page never cleared the engine list', () => calls.length === 1);
			assert.deepEqual(calls[0], []);
			mod.noteEngineBoot('boot-a');
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(calls.length, 1, 'the same boot must not clear twice');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a page that never published a chain still clears once the engine boot is known', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			await until('the fresh page never cleared the engine list', () => calls.length === 1);
			assert.deepEqual(calls[0], []);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a daemon with no boot id still gets the one initial clear', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot(null);
			await until('the legacy daemon was never cleared', () => calls.length === 1);
			assert.deepEqual(calls[0], []);
			mod.noteEngineBoot(null);
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(calls.length, 1, 'no boot id means no restart can be told apart');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('an empty boot id is refused', async () => {
		const mod = await loadNextUp();
		assert.throws(() => mod.noteEngineBoot(''), /boot id/);
	});

	test('a boot seen while a PUT is in flight resends the chain to the restarted engine', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		let release = () => {};
		const held = new Promise((resolve) => {
			release = resolve;
		});
		const calls = installFetch((ids, n) => (n === 3 ? held.then(() => jsonOk(ids)) : jsonOk(ids)));
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await until('A never left the page', () => calls.length === 1);
			await new Promise((resolve) => setTimeout(resolve, 20));
			mod.noteEngineBoot('boot-a');
			await until('the first boot did not resend A', () => calls.length === 2);
			await new Promise((resolve) => setTimeout(resolve, 20));
			mod.publishAutoPlayNextUp(['track-b']);
			await until('B never went in flight', () => calls.length === 3);
			mod.noteEngineBoot('boot-b');
			release();
			await until('the restarted engine was never told B', () => calls.length >= 4);
			assert.deepEqual(calls[3], ['track-b']);
			await new Promise((resolve) => setTimeout(resolve, 60));
			assert.equal(calls.length, 4, 'one resend per boot, not a loop');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('clearing a sent chain PUTs an empty next-up', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch(() => jsonOk([]));
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await until('chain never left the page', () => calls.length === 1);
			mod.publishAutoPlayNextUp([]);
			await until('clear never left the page', () => calls.length === 2);
			assert.deepEqual(calls[0], ['track-a']);
			assert.deepEqual(calls[1], []);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a lost empty clear retries the empty chain', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((_ids, n) =>
			n === 2 ? Promise.reject(new TypeError('response lost')) : jsonOk(_ids)
		);
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await until('chain never left the page', () => calls.length === 1);
			mod.publishAutoPlayNextUp([]);
			await until('lost clear did not retry the empty chain', () => calls.length >= 3);
			assert.deepEqual(calls[1], []);
			assert.deepEqual(calls[2], []);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a lost B response retries B while the page still wants B', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((_ids, n) =>
			n === 2 ? Promise.reject(new TypeError('response lost')) : jsonOk(_ids)
		);
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await until('accepted A never left the page', () => calls.length === 1);
			await new Promise((resolve) => setTimeout(resolve, 20));
			mod.publishAutoPlayNextUp(['track-b']);
			await until('lost B did not retry B', () => calls.length >= 3);
			assert.deepEqual(calls[1], ['track-b']);
			assert.deepEqual(calls[2], ['track-b']);
		} finally {
			globalThis.fetch = previous;
		}
	});
});

test('a later engine boot resends the chain the page still wants', async () => {
  const mod = await loadNextUp();
  const previous = globalThis.fetch;
  const calls = installFetch(() => jsonOk(['track-a']));
  try {
    mod.noteEngineBoot('boot-a');
    mod.publishAutoPlayNextUp(['track-a']);
    await until('the initial clear and the first next-up send', () => calls.length === 2);
    assert.deepEqual(calls[0], []);
    assert.deepEqual(calls[1], ['track-a']);
    mod.noteEngineBoot('boot-a');
    await new Promise((resolve) => setTimeout(resolve, 40));
    assert.equal(calls.length, 2);
    mod.noteEngineBoot('boot-b');
    await until('the restarted engine was told the chain', () => calls.length === 3);
    assert.deepEqual(calls[2], ['track-a']);
  } finally {
    globalThis.fetch = previous;
  }
});

test('the first observed boot resends a chain that was already accepted', async () => {
  const mod = await loadNextUp();
  const previous = globalThis.fetch;
  const calls = installFetch(() => jsonOk(['track-a']));
  try {
    mod.publishAutoPlayNextUp(['track-a']);
    await until('the chain was sent before any boot id', () => calls.length === 1);
    mod.noteEngineBoot('boot-new');
    await until('the first boot id resent the chain', () => calls.length === 2);
    assert.deepEqual(calls[1], ['track-a']);
  } finally {
    globalThis.fetch = previous;
  }
});

/**
 * A refused or unreachable engine is asked a bounded number of times.
 *
 * [if] the same chain is published again and again
 *   [then] one PUT [else the engine is told what it already holds].
 * [if] every PUT fails
 *   [then] the page stops after its retry schedule and says so
 *   [else an engine that keeps refusing is asked every few seconds forever].
 * [if] the chain changes, or a new engine boot is seen, after the page stopped
 *   [then] it sends again [else one bad minute silences next-up for the session].
 */
const TEN_MINUTES_MS = 10 * 60 * 1000;

async function settle() {
	// A failed PUT reaches the retry scheduler through several promise hops.
	for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function advance(t, ms, stepMs = 500) {
	for (let elapsed = 0; elapsed < ms; elapsed += stepMs) {
		t.mock.timers.tick(stepMs);
		await settle();
	}
}

/** The next-up client's own lines at one console level. Node writes its own
 * warnings there too (mock timers are experimental on Node 22), and those are
 * not the client's. */
function captureConsole(t, level) {
	const lines = [];
	t.mock.method(console, level, (...parts) => {
		const line = parts.map(String).join(' ');
		if (line.startsWith('[autoplay]')) lines.push(line);
	});
	return lines;
}

const refusals = {
	'the connection fails': () => Promise.reject(new TypeError('fetch failed')),
	'the engine answers 503 not armed': () =>
		new Response(JSON.stringify({ detail: { code: 'AHEAD_ANALYSIS_NOT_ARMED' } }), {
			status: 503,
			headers: { 'content-type': 'application/json' }
		})
};

describe('next-up sends are bounded', { concurrency: 1 }, () => {
	test('the same chain published many times is sent once', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			for (let i = 0; i < 25; i += 1) mod.publishAutoPlayNextUp(['track-a', 'track-b']);
			await until('the chain never left the page', () => calls.length === 1);
			for (let i = 0; i < 25; i += 1) mod.publishAutoPlayNextUp(['track-a', 'track-b']);
			await new Promise((resolve) => setTimeout(resolve, 80));
			assert.deepEqual(calls, [['track-a', 'track-b']], 'an unchanged chain must not be sent again');
			// the control: a changed chain still sends
			mod.publishAutoPlayNextUp(['track-b', 'track-c']);
			await until('the changed chain never left the page', () => calls.length === 2);
			assert.deepEqual(calls[1], ['track-b', 'track-c']);
		} finally {
			globalThis.fetch = previous;
		}
	});

	for (const [how, refuse] of Object.entries(refusals)) {
		test(`when ${how} the page stops asking and says so`, async (t) => {
			const previous = globalThis.fetch;
			const mod = await loadNextUp();
			captureConsole(t, 'warn');
			const stopped = captureConsole(t, 'error');
			t.mock.timers.enable({ apis: ['setTimeout'] });
			const calls = installFetch(() => refuse());
			try {
				mod.noteEngineBoot('boot-a');
				mod.publishAutoPlayNextUp(['track-a']);
				await settle();
				await advance(t, TEN_MINUTES_MS);
				const asked = calls.length;
				assert.ok(asked >= 3, `a failed send must still be retried: ${asked} request(s)`);
				assert.ok(asked <= 12, `a failing engine must not be asked without end: ${asked} request(s) in ten minutes`);
				assert.equal(asked, mod.RETRY_DELAYS_MS.length + 1, 'one send, then one retry per scheduled delay');
				// The boot's own clear went first and failed; every retry carries the chain the page wants.
				assert.deepEqual(calls[0], []);
				assert.ok(calls.slice(1).every((ids) => ids.length === 1 && ids[0] === 'track-a'), JSON.stringify(calls));
				assert.equal(stopped.length, 1, `the stop must be reported once: ${JSON.stringify(stopped)}`);
				assert.match(stopped[0], /stopped/);

				// An unchanged chain and the same boot do not restart the loop.
				mod.publishAutoPlayNextUp(['track-a']);
				mod.noteEngineBoot('boot-a');
				await advance(t, TEN_MINUTES_MS);
				assert.equal(calls.length, asked, 'the same chain on the same engine must stay stopped');

				// The controls: a changed chain sends again, and so does a new boot.
				mod.publishAutoPlayNextUp(['track-b']);
				await settle();
				assert.deepEqual(calls[asked], ['track-b'], 'a changed chain must send after a stop');
				await advance(t, TEN_MINUTES_MS);
				const afterChange = calls.length;
				assert.equal(afterChange, asked * 2, 'the changed chain gets its own bounded schedule');
				mod.noteEngineBoot('boot-b');
				await settle();
				assert.equal(calls.length, afterChange + 1, 'a new engine boot must send after a stop');
			} finally {
				globalThis.fetch = previous;
			}
		});
	}

	test('a send that succeeds after failures starts the next failure from the short delay', async (t) => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		captureConsole(t, 'warn');
		const stopped = captureConsole(t, 'error');
		t.mock.timers.enable({ apis: ['setTimeout'] });
		let failing = true;
		const calls = installFetch((ids) => (failing ? Promise.reject(new TypeError('fetch failed')) : jsonOk(ids)));
		try {
			mod.publishAutoPlayNextUp(['track-a']);
			await settle();
			await advance(t, 1000, 50);
			assert.ok(calls.length >= 3 && calls.length <= mod.RETRY_DELAYS_MS.length, `${calls.length} request(s)`);
			failing = false;
			await advance(t, TEN_MINUTES_MS);
			const accepted = calls.length;
			assert.equal(stopped.length, 0, 'a recovered engine is not a stop');
			failing = true;
			mod.publishAutoPlayNextUp(['track-b']);
			await settle();
			await advance(t, mod.RETRY_DELAYS_MS[0], 10);
			assert.equal(calls.length, accepted + 2, 'the first retry after a recovery comes at the first delay');
		} finally {
			globalThis.fetch = previous;
		}
	});
});

//-----------------------------------------------------------------------------
// first-use loader (autoplay-next-up-lazy.ts): off "/" first paint, same behavior
//-----------------------------------------------------------------------------

async function loadLazy(options = {}) {
	const lazy = await loadTypeScriptModule('src/lib/rb/autoplay-next-up-lazy.ts', {
		viteApiBase: 'http://127.0.0.1:9',
		...options
	});
	lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
	return lazy;
}

describe('next-up first-use loader', { concurrency: 1 }, () => {
	test('calls reach the client in the order they were made, with the chain as it was then', async () => {
		const previous = globalThis.fetch;
		async function drive(mod) {
			const calls = installFetch((ids) => jsonOk(ids));
			const chain = ['track-a', 'track-b', 'track-c'];
			mod.publishAutoPlayNextUp(chain);
			chain.splice(0, chain.length, 'track-z');
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-c']);
			await until('the last chain never reached the engine', () => calls.at(-1)?.[0] === 'track-c');
			await new Promise((resolve) => setTimeout(resolve, 40));
			return calls;
		}
		try {
			// CONTROL: the static client, driven the same way, is what "no change" means.
			const direct = await drive(await loadNextUp());
			const viaLoader = await drive(await loadLazy());
			assert.deepEqual(viaLoader, direct);
			assert.deepEqual(viaLoader[0], ['track-a', 'track-b'], 'a later mutation must not change what was published');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('an empty boot id still throws at the call, so the health probe reports it', async () => {
		const lazy = await loadLazy();
		assert.throws(() => lazy.noteEngineBoot(''), /engine boot id is empty/);
	});

	test('a failed client load is not cached: the next call loads again', async () => {
		const { onceUntilFailure } = await loadTypeScriptModule('src/lib/rb/autoplay-next-up-lazy.ts', {
			viteApiBase: 'http://127.0.0.1:9'
		});
		let loads = 0;
		const load = onceUntilFailure(async () => {
			loads += 1;
			if (loads === 1) throw new TypeError('Failed to fetch dynamically imported module');
			return 'client';
		});
		await assert.rejects(load(), /Failed to fetch/);
		assert.equal(await load(), 'client', 'if a failed chunk load is cached then the client is gone for the session');
		assert.equal(loads, 2);
	});

	test('CONTROL: a client that loaded is loaded once, however many calls share it', async () => {
		const { onceUntilFailure } = await loadTypeScriptModule('src/lib/rb/autoplay-next-up-lazy.ts', {
			viteApiBase: 'http://127.0.0.1:9'
		});
		let loads = 0;
		const load = onceUntilFailure(async () => {
			loads += 1;
			return 'client';
		});
		assert.deepEqual(await Promise.all([load(), load(), load()]), ['client', 'client', 'client']);
		assert.equal(await load(), 'client');
		assert.equal(loads, 1);
	});

	/** A loader whose first load rejects, as a failed chunk fetch does, and later loads give `client`. */
	async function flakyLoader(failures = 1) {
		const { lazyNextUpClient } = await loadTypeScriptModule('src/lib/rb/autoplay-next-up-lazy.ts', {
			viteApiBase: 'http://127.0.0.1:9'
		});
		const client = await loadNextUp({ lease: null });
		let loads = 0;
		const lazy = lazyNextUpClient(async () => {
			loads += 1;
			if (loads <= failures) throw new TypeError('Failed to fetch dynamically imported module');
			return client;
		});
		return lazy;
	}

	test('a leadership bind whose chunk load failed is replayed by the next load that succeeds', async () => {
		const previous = globalThis.fetch;
		const original = console.error;
		console.error = () => {};
		try {
			const lazy = await flakyLoader();
			const calls = installFetch((ids) => jsonOk(ids));
			lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			await new Promise((resolve) => setTimeout(resolve, 20));
			lazy.publishAutoPlayNextUp(['track-a']);
			await until('the replayed bind never let the leader write', () => calls.at(-1)?.[0] === 'track-a');
		} finally {
			console.error = original;
			globalThis.fetch = previous;
		}
	});

	test('the chart and boot a failed load dropped are replayed by the next load that succeeds', async () => {
		const previous = globalThis.fetch;
		const original = console.error;
		console.error = () => {};
		try {
			const lazy = await flakyLoader(1);
			const calls = installFetch((ids) => jsonOk(ids));
			lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			lazy.publishAutoPlayNextUp(['track-a']);
			await new Promise((resolve) => setTimeout(resolve, 20));
			assert.deepEqual(calls, [], 'the shared load failed, so nothing was sent yet');
			// The next health probe reads the same boot; the chart has not changed.
			lazy.noteEngineBoot('boot-a');
			await until('the dropped chart was never replayed', () => calls.some((c) => c[0] === 'track-a'));
		} finally {
			console.error = original;
			globalThis.fetch = previous;
		}
	});

	test('a boot a failed load dropped is replayed, so the next empty chart still clears the engine', async () => {
		const previous = globalThis.fetch;
		const original = console.error;
		console.error = () => {};
		try {
			const lazy = await flakyLoader(1);
			const calls = installFetch((ids) => jsonOk(ids));
			lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			lazy.noteEngineBoot('boot-a');
			await new Promise((resolve) => setTimeout(resolve, 20));
			// Without the boot the client cannot know an engine may hold a list,
			// and an empty chart would send nothing.
			lazy.publishAutoPlayNextUp([]);
			await until('the dropped boot was never replayed', () => calls.length === 1);
			assert.deepEqual(calls, [[]]);
		} finally {
			console.error = original;
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: a chart replaced while loads failed is not replayed, only the latest one', async () => {
		const previous = globalThis.fetch;
		const original = console.error;
		console.error = () => {};
		try {
			const lazy = await flakyLoader(1);
			const calls = installFetch((ids) => jsonOk(ids));
			lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			lazy.publishAutoPlayNextUp(['track-a']);
			lazy.publishAutoPlayNextUp(['track-b']);
			await new Promise((resolve) => setTimeout(resolve, 20));
			// A remount binds again; with no boot to send first, a replayed
			// replaced chart would go out at once.
			lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			await until('the latest chart was never replayed', () => calls.at(-1)?.[0] === 'track-b');
			await new Promise((resolve) => setTimeout(resolve, 60));
			assert.ok(!calls.some((c) => c[0] === 'track-a'), `a replaced chart was sent: ${JSON.stringify(calls)}`);
		} finally {
			console.error = original;
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: a bind undone before any load succeeds is not replayed', async () => {
		const previous = globalThis.fetch;
		const original = console.error;
		console.error = () => {};
		try {
			const lazy = await flakyLoader();
			const calls = installFetch((ids) => jsonOk(ids));
			const unbind = lazy.bindNextUpLeadership(fakeLease(true), 'tab-a');
			await new Promise((resolve) => setTimeout(resolve, 20));
			unbind();
			lazy.publishAutoPlayNextUp(['track-a']);
			await new Promise((resolve) => setTimeout(resolve, 80));
			assert.deepEqual(calls, [], 'an unmounted route must not start writing');
		} finally {
			console.error = original;
			globalThis.fetch = previous;
		}
	});

	test('a client chunk that cannot load is logged, never thrown into the caller', async () => {
		const errors = [];
		const original = console.error;
		console.error = (...args) => errors.push(args);
		try {
			const lazy = await loadTypeScriptModule('src/lib/rb/autoplay-next-up-lazy.ts', {
				viteApiBase: 'http://127.0.0.1:9',
				failImports: [/autoplay-next-up$/]
			});
			assert.doesNotThrow(() => lazy.publishAutoPlayNextUp(['track-a']));
			await until('the failed load was not logged', () => errors.length === 1);
			assert.match(String(errors[0][0]), /next-up analysis client failed to load/);
		} finally {
			console.error = original;
		}
	});
});

//-----------------------------------------------------------------------------
// one writer per engine: the confirmed leader tab (AGENT-18)
//-----------------------------------------------------------------------------

describe('next-up leader gate', { concurrency: 1 }, () => {
	test('a tab with no leadership bound (the library desk) writes nothing, not even the boot clear', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp({ lease: null });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-a']);
			await new Promise((resolve) => setTimeout(resolve, 80));
			assert.deepEqual(calls, []);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a follower tab neither publishes nor clears the playing tab list', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp({ lease: fakeLease(false) });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-a']);
			await new Promise((resolve) => setTimeout(resolve, 80));
			assert.deepEqual(calls, [], 'the follower would have overwritten the leader list');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a tab that becomes leader sends the chain it wants, even one it sent before it lost the lease', async () => {
		const previous = globalThis.fetch;
		const lease = fakeLease(true);
		const mod = await loadNextUp({ lease });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-a']);
			await until('the leader never sent its chain', () => calls.at(-1)?.[0] === 'track-a');
			await new Promise((resolve) => setTimeout(resolve, 20));
			const sent = calls.length;
			lease.leads(false);
			// Another tab led meanwhile and wrote its own list over this one.
			lease.leads(true);
			await until('the new leader did not republish', () => calls.length === sent + 1);
			assert.deepEqual(calls.at(-1), ['track-a']);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('a tab that becomes leader with no chart clears the previous leader list', async () => {
		const previous = globalThis.fetch;
		const lease = fakeLease(false);
		const mod = await loadNextUp({ lease });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(calls.length, 0);
			lease.leads(true);
			await until('the promoted tab sent nothing', () => calls.length === 1);
			assert.deepEqual(calls[0], []);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('every next-up PUT names this tab with the ui-mirror lease header, so the engine can refuse a former leader', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp();
		const leases = [];
		globalThis.fetch = async (input, init) => {
			const headers = new Headers(input instanceof Request ? input.headers : init?.headers);
			leases.push(headers.get('x-opendj-lease'));
			return jsonOk(await stableIdsOf(input, init));
		};
		try {
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-a']);
			await until('the leader never sent its chain', () => leases.length === 2);
			assert.deepEqual(leases, ['tab-a', 'tab-a']);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: a confirmation notice that keeps the same leader sends nothing new', async () => {
		const previous = globalThis.fetch;
		const lease = fakeLease(true);
		const mod = await loadNextUp({ lease });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.noteEngineBoot('boot-a');
			mod.publishAutoPlayNextUp(['track-a']);
			await until('the leader never sent its chain', () => calls.at(-1)?.[0] === 'track-a');
			await new Promise((resolve) => setTimeout(resolve, 20));
			const sent = calls.length;
			lease.leads(true);
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(calls.length, sent);
		} finally {
			globalThis.fetch = previous;
		}
	});

	/** A leader bound by hand, so the test holds its unbind, that has sent `track-a`. */
	async function boundLeaderThatSent() {
		const mod = await loadNextUp({ lease: null });
		const calls = installFetch((ids) => jsonOk(ids));
		const unbind = mod.bindNextUpLeadership(fakeLease(true), 'tab-a');
		mod.noteEngineBoot('boot-a');
		mod.publishAutoPlayNextUp(['track-a']);
		await until('the leader never sent its chain', () => calls.at(-1)?.[0] === 'track-a');
		await new Promise((resolve) => setTimeout(resolve, 20));
		return { mod, calls, unbind };
	}

	test('a route that rebinds a lease that already leads republishes the chain it sent before unbinding', async () => {
		const previous = globalThis.fetch;
		try {
			const { mod, calls, unbind } = await boundLeaderThatSent();
			const sent = calls.length;
			unbind();
			// While nothing was bound another tab led and wrote its own list.
			mod.bindNextUpLeadership(fakeLease(true), 'tab-a');
			await until('the rebound leader trusted the list it sent before', () => calls.length === sent + 1);
			assert.deepEqual(calls.at(-1), ['track-a']);
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(calls.length, sent + 1, 'one republish, not a resend loop');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: a route that rebinds a lease that does not lead sends nothing', async () => {
		const previous = globalThis.fetch;
		try {
			const { mod, calls, unbind } = await boundLeaderThatSent();
			const sent = calls.length;
			unbind();
			mod.bindNextUpLeadership(fakeLease(false), 'tab-a');
			await new Promise((resolve) => setTimeout(resolve, 60));
			assert.equal(calls.length, sent, 'a follower would have overwritten the leader list');
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: binding a leading lease before any engine is known sends nothing until the boot', async () => {
		const previous = globalThis.fetch;
		const mod = await loadNextUp({ lease: null });
		const calls = installFetch((ids) => jsonOk(ids));
		try {
			mod.bindNextUpLeadership(fakeLease(true), 'tab-a');
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.deepEqual(calls, [], 'an empty clear has nowhere to go before an engine is seen');
			mod.noteEngineBoot('boot-a');
			await until('the boot clear never went', () => calls.length === 1);
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.deepEqual(calls, [[]], 'exactly the one boot clear');
		} finally {
			globalThis.fetch = previous;
		}
	});

	/** A leader whose next PUT of `track-a` hangs until the test answers it. */
	async function heldAck() {
		const lease = fakeLease(true);
		const mod = await loadNextUp({ lease });
		let answer = () => {};
		const calls = installFetch((ids) =>
			ids[0] === 'track-a' && calls.filter((c) => c[0] === 'track-a').length === 1
				? new Promise((resolve) => {
						answer = () => resolve(jsonOk(ids));
					})
				: jsonOk(ids)
		);
		mod.noteEngineBoot('boot-a');
		mod.publishAutoPlayNextUp(['track-a']);
		await until('track-a never went in flight', () => calls.at(-1)?.[0] === 'track-a');
		return { lease, calls, ack: () => answer() };
	}

	test('an ack from before the tab lost and regained the lease confirms nothing, so the chain is sent again', async () => {
		const previous = globalThis.fetch;
		try {
			const { lease, calls, ack } = await heldAck();
			const sent = calls.length;
			lease.leads(false);
			// Another tab led meanwhile and wrote its own list over this one.
			lease.leads(true);
			ack();
			await until('the regained leader trusted an earlier tenure ack', () => calls.length === sent + 1);
			assert.deepEqual(calls.at(-1), ['track-a']);
		} finally {
			globalThis.fetch = previous;
		}
	});

	test('CONTROL: an ack in the same tenure confirms the chain and sends nothing more', async () => {
		const previous = globalThis.fetch;
		try {
			const { lease, calls, ack } = await heldAck();
			const sent = calls.length;
			lease.leads(true);
			ack();
			await new Promise((resolve) => setTimeout(resolve, 60));
			assert.equal(calls.length, sent);
		} finally {
			globalThis.fetch = previous;
		}
	});
});

//-----------------------------------------------------------------------------
// the live acceptance client (tests/live/arm-autoplay-next-up.ts)
//-----------------------------------------------------------------------------

test('the live AutoPlay acceptance client PUTs the charted head as the confirmed leader', async () => {
	const previous = globalThis.fetch;
	let stored = [];
	const puts = [];
	globalThis.fetch = async (input, init) => {
		const method = input instanceof Request ? input.method : init?.method ?? 'GET';
		const url = input instanceof Request ? input.url : String(input);
		if (method === 'GET' && url.includes('/api/v1/ahead-analysis/coverage')) {
			return new Response(JSON.stringify({ while_playing: { next_up: stored } }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		const ids = await stableIdsOf(input, init);
		puts.push(ids);
		stored = ids;
		return jsonOk(ids);
	};
	try {
		const live = await loadTypeScriptModule('tests/live/arm-autoplay-next-up.ts', {
			viteApiBase: 'http://127.0.0.1:9'
		});
		const head = await live.armChartedNextUp(['track-a', 'track-b', 'track-c'], ['track-b', 'track-c', 'track-a'], 'page-a');
		assert.deepEqual(head, ['track-b', 'track-c']);
		assert.deepEqual(puts, [['track-b', 'track-c']], 'the client must send exactly the charted head');
	} finally {
		globalThis.fetch = previous;
	}
});
