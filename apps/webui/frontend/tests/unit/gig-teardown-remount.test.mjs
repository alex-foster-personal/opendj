// [if] a Gig teardown hard-mutes the master and a remount claims the engine before
// the release disposes it (the Library -> Gig in-app nav mounts /performance twice)
// [then] the remount gets its master volume back [⛔️ if the new Gig plays into a
// master gain of 0 and the silence watchdog cuts the deck ~2 s in].
//
// AGENTS.md's no-mocks contract (L148-152): the master-write revision this
// module relies on is production code (`recordMasterWrite` /
// `readMasterWriteRevision` in state.svelte.ts, incremented from inside the
// real `engine.setMaster`), not a test-local counter. Exercising a fake
// counter and fake setter would let a break in the real wiring (removing or
// bypassing `recordMasterWrite()`) leave every test here green
// (discussion_r4119767451). `disposeEngine` alone stays a stand-in: a real
// `engine.dispose()` needs a browser AudioContext this test environment does
// not have, so it is faked to reproduce exactly what the real dispose path
// does to the master -- write `mixerState.master = 1` directly, bypassing
// `engine.setMaster` and therefore never bumping the revision (matching
// audio-engine.svelte.ts's dispose(), which resets `mixerState.master`
// without going through the tracked setter).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { installFakeAudioGraphGlobals, lastCreatedFakeAudioContext } from './fixtures/fake-audio-context.mjs';

const runtime = await loadTypeScriptModule('src/lib/rb/library-mode-runtime.ts');
const teardown = await loadTypeScriptModule('src/lib/rb/performance-route-teardown.ts');
// One shared-graph entry (see the fixture's own comment): `engine` and
// `mixerState`/`readMasterWriteRevision` must come from the SAME bundle, or
// `engine.setMaster`'s recordMasterWrite() bumps a revision counter this
// test can never observe.
const { engine, ensureAudioGraphForCue, mixerState, readMasterWriteRevision } = await loadTypeScriptModule(
	'tests/unit/fixtures/gig-teardown-master-entry.ts'
);

function realMixer(initialMaster) {
	mixerState.master = initialMaster;
	const writes = [];
	return {
		writes,
		readMaster: () => mixerState.master,
		readMasterWriteRevision,
		// Routes every write through the real production setter, so the
		// revision this module's restoration logic checks is the same one
		// `engine.setMaster` actually maintains, not a reimplementation of it.
		setMaster: (value) => {
			writes.push(value);
			engine.setMaster(value);
		}
	};
}

function gigTeardown(fake, disposeCalls, onDispose = () => {}) {
	return teardown.createFailClosedPerformanceTeardown({
		...runtime.createGigTeardownActions({
			readMaster: fake.readMaster,
			readMasterWriteRevision: fake.readMasterWriteRevision,
			setMaster: fake.setMaster,
			disposeEngine: async () => {
				disposeCalls.push('dispose');
				// The REAL engine.dispose(), not a hand-rolled `mixerState.master =
				// 1` stand-in (Sol P1, PR #4034, discussion_r4129483225): it needs
				// no live AudioContext (guards on _masterGain/_ctx both being
				// nullable, exactly like deck-snapshot-beatgrid-memo.test.mjs's
				// projectFourThenUnload()), so faking its master-reset side effect
				// let a break in the real wiring pass here unnoticed. It resets
				// mixerState.master = 1 directly, never through setMaster, so it
				// never bumps the revision -- unchanged behavior, now proven by the
				// production code path instead of assumed.
				await engine.dispose();
				onDispose();
			}
		}),
		gracefulStop: async () => {},
		reportError: (phase, error) => {
			throw new Error(`unexpected ${phase} failure: ${String(error)}`);
		}
	});
}

test('a teardown superseded by a remount before disposal restores the master it muted', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	const pending = gigTeardown(fake, disposeCalls)();
	assert.equal(mixerState.master, 0, 'the hard mute is taken synchronously');
	runtime.noteGigRuntimeMounted(); // the second mount lands before the release's first await resolves
	await pending;

	assert.deepEqual(disposeCalls, [], 'the remounted engine is not disposed');
	assert.equal(mixerState.master, 0.8, 'the remount inherits the pre-teardown master, not the mute');
	assert.notEqual(globalThis.window.__mdtLibraryModeIdle, true);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a completed Library teardown preserves the master for the next Gig mount', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	await gigTeardown(fake, disposeCalls)();

	assert.deepEqual(disposeCalls, ['dispose']);
	assert.deepEqual(fake.writes, [0, 0.8], 'the state is muted for disposal, then re-armed for Gig');
	assert.equal(mixerState.master, 0.8, 'the next Gig inherits the pre-Library master');
	assert.equal(globalThis.window.__mdtLibraryModeIdle, true);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('control: a remount that lands after disposal does not reapply the old master', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	// The remount lands after dispose, and its operator pulls the master to 0
	// through the real setter -- a deliberate write always does, in the app.
	await gigTeardown(fake, disposeCalls, () => {
		runtime.noteGigRuntimeMounted();
		fake.setMaster(0);
	})();

	assert.deepEqual(disposeCalls, ['dispose']);
	assert.equal(mixerState.master, 0, 'a disposed release owes the remount nothing, so a deliberate 0 stands');
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('control: a remount that already moved the master off the mute is not overwritten', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	const pending = gigTeardown(fake, disposeCalls)();
	runtime.noteGigRuntimeMounted();
	fake.setMaster(0.3); // the new mount (or the operator) set a volume in the gap
	await pending;

	assert.equal(mixerState.master, 0.3);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a superseding remount can deliberately keep the master muted', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	const pending = gigTeardown(fake, disposeCalls)();
	runtime.noteGigRuntimeMounted();
	fake.setMaster(0); // a real write from the remount, not the older teardown's mute
	await pending;

	assert.deepEqual(fake.writes, [0, 0], 'the superseding zero write is observable');
	assert.equal(mixerState.master, 0, 'the stale teardown does not overwrite a deliberate mute');
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a remount that lands WHILE disposal is still in flight still gets its master back', async () => {
	// discussion_r4119767447: the generation check right after `await
	// disposeEngine()` used to return without calling either callback, so a
	// remount landing in that exact window (after the pre-dispose check, but
	// before disposeEngine() resolves) lost masterBeforeMute for good: the
	// real dispose() had already reset mixerState.master to its default (1)
	// with nothing left to restore it.
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	// The remount happens FROM INSIDE disposeEngine, i.e. while it is
	// in-flight and after the pre-dispose generation check already passed.
	const pending = gigTeardown(fake, disposeCalls, () => {
		runtime.noteGigRuntimeMounted();
	})();
	await pending;

	assert.deepEqual(disposeCalls, ['dispose'], 'disposal still runs: the remount lands too late to skip it');
	assert.equal(
		mixerState.master,
		0.8,
		'onDisposedAfterSupersession restores the pre-mute master the disposal reset'
	);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a second overlapping teardown does not treat the first teardown\'s temporary mute as the original', async () => {
	// Sol review, PR #4034, library-mode-runtime.ts:210: a rapid Gig ->
	// Library navigation during the documented double-mount transition can
	// unmount a SECOND Gig instance (its own createGigTeardownActions
	// closure) before the FIRST instance's release has resumed from
	// disposeStemDecoderPools(). Instance B's hardMute() must not read
	// instance A's temporary 0 as "the" master to protect.
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCallsA = [];
	const disposeCallsB = [];

	let releaseA;
	const aMayFinish = new Promise((resolve) => {
		releaseA = resolve;
	});
	const teardownA = teardown.createFailClosedPerformanceTeardown({
		...runtime.createGigTeardownActions({
			readMaster: fake.readMaster,
			readMasterWriteRevision: fake.readMasterWriteRevision,
			setMaster: fake.setMaster,
			disposeEngine: async () => {
				disposeCallsA.push('dispose');
				await aMayFinish;
				// The REAL engine.dispose(), not a hand-rolled `mixerState.master =
				// 1` stand-in (Codex review, PR #4034): faking the reset let this
				// test stay green through a change to dispose()'s own reset
				// ordering or its behavior under concurrent disposal -- the exact
				// interleaving this case exists to protect. `aMayFinish` still
				// gates WHEN disposal resumes, so the overlap timing this test
				// drives is unchanged; only the reset itself now runs the
				// production path, same as gigTeardown()'s default disposeEngine.
				await engine.dispose();
			}
		}),
		gracefulStop: async () => {},
		reportError: (phase, error) => {
			throw new Error(`unexpected ${phase} failure (A): ${String(error)}`);
		}
	});
	const pendingA = teardownA(); // hardMute() runs synchronously here: master 0.8 -> 0

	assert.equal(fake.readMaster(), 0, 'A\'s mute takes effect');

	// A second Gig instance mounts (bumping the ownership generation, exactly
	// as a real double-mount does) and then unmounts before A's disposal
	// resumes. Its own createGigTeardownActions() closure has no memory of
	// A's true original -- only the shared module state does.
	runtime.noteGigRuntimeMounted();
	await gigTeardown(fake, disposeCallsB)();

	assert.equal(
		fake.readMaster(),
		0.8,
		'B must restore the TRUE original master (0.8), not the 0 it read at its own hardMute()'
	);

	releaseA();
	await pendingA;
	assert.equal(fake.readMaster(), 0.8, 'A finishing afterward must not clobber the already-correct restore');

	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('control: a remount landing during disposal does not overwrite a deliberate write made in that window', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCalls = [];

	const pending = gigTeardown(fake, disposeCalls, () => {
		runtime.noteGigRuntimeMounted();
		fake.setMaster(0.3); // the newer mount (or the operator) sets a real value mid-disposal
	})();
	await pending;

	assert.deepEqual(disposeCalls, ['dispose']);
	assert.equal(
		mixerState.master,
		0.3,
		'a real write after the mute, even mid-disposal, must never be clobbered by the stale restore'
	);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a stale capture invalidated by an external write does not poison the next hardMute', async () => {
	// Sol review, PR #4034, discussion_r4128777355: when a teardown is
	// superseded (a remount bumps the release generation) and something
	// writes a DIFFERENT master value without going through hardMute() again
	// (an unrelated fader move on the new Gig, not a re-mute), the stale
	// teardown's onDisposeSkipped correctly declines to restore -- its
	// revision no longer matches -- but must also CLEAR the pending capture.
	// Left populated, a LATER hardMute() on this same still-live Gig treats
	// it as "already captured" and skips reading its own true current value,
	// then restores the abandoned one instead.
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);
	const disposeCallsA = [];

	const teardownA = teardown.createFailClosedPerformanceTeardown({
		...runtime.createGigTeardownActions({
			readMaster: fake.readMaster,
			readMasterWriteRevision: fake.readMasterWriteRevision,
			setMaster: fake.setMaster,
			disposeEngine: async () => {
				disposeCallsA.push('dispose');
			}
		}),
		gracefulStop: async () => {},
		reportError: (phase, error) => {
			throw new Error(`unexpected ${phase} failure (A): ${String(error)}`);
		}
	});
	const pendingA = teardownA(); // hardMute() runs synchronously: master 0.8 -> 0

	// A remount supersedes A's release, but the new Gig does not re-mute --
	// it just writes its own real volume, exactly like an operator's fader.
	runtime.noteGigRuntimeMounted();
	fake.setMaster(0.3);
	await pendingA;

	assert.deepEqual(disposeCallsA, [], 'A is superseded before its disposeEngine() is ever called');
	assert.equal(fake.readMaster(), 0.3, 'the correct decline: 0.3 must survive A\'s superseded teardown');

	// The newer Gig's OWN, later teardown: its hardMute() must capture the
	// CURRENT 0.3, not inherit A's abandoned 0.8.
	const disposeCallsB = [];
	await gigTeardown(fake, disposeCallsB)();

	assert.deepEqual(disposeCallsB, ['dispose']);
	assert.equal(
		fake.readMaster(),
		0.3,
		'B must restore its OWN true original (0.3), not A\'s stale, already-invalidated capture (0.8)'
	);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('a disposal that rejects still restores the pre-mute master, and still propagates', async () => {
	// Codex review, PR #4034, discussion_r4130444130 (superseding the earlier
	// Sol P1 at discussion_r4129826047): a hand-rolled `disposeEngine` that
	// sets `mixerState.master = 1` then throws only reproduces what
	// audio-engine.svelte.ts's real dispose() does today. It would stay green
	// through a change to that ordering or to what a rejecting teardown
	// leaves behind. This drives the REAL `engine.dispose()` -- a genuinely
	// built graph (via `ensureAudioGraphForCue()`, the same production seam
	// the library preview uses before any deck has loaded) whose
	// `AudioContext.close()` is made to reject -- so the assertions below
	// hold only because the production path actually behaves this way.
	runtime.resetLibraryModeRuntimeForTest();
	const uninstall = installFakeAudioGraphGlobals();
	try {
		await ensureAudioGraphForCue();
		const ctx = lastCreatedFakeAudioContext();
		ctx.close = async () => {
			throw new Error('AudioContext.close() failed');
		};

		const fake = realMixer(0.8);
		const disposeCalls = [];

		const actions = runtime.createGigTeardownActions({
			readMaster: fake.readMaster,
			readMasterWriteRevision: fake.readMasterWriteRevision,
			setMaster: fake.setMaster,
			disposeEngine: async () => {
				disposeCalls.push('dispose');
				await engine.dispose();
			}
		});
		actions.hardMute();
		assert.equal(fake.readMaster(), 0, 'the mute takes effect synchronously');

		await assert.rejects(() => actions.dispose(), /AudioContext.close\(\) failed/);

		assert.deepEqual(disposeCalls, ['dispose']);
		assert.equal(fake.readMaster(), 0.8, 'the pre-mute master is restored even though disposal rejected');
	} finally {
		uninstall();
		runtime.resetLibraryModeRuntimeForTest();
	}
});

test('hardMute recaptures the current master when the pending capture has gone stale', async () => {
	// Sol review, PR #4034, discussion_r4130356762: A saves master 0.8 and is
	// awaiting stem-pool disposal. A live second instance writes a REAL
	// value, 0.3, then itself mutes and tears down before A's callback runs.
	// Because the pending capture was non-null, the second instance's own
	// hardMute() used to skip reading the current 0.3, reusing A's stale 0.8
	// and re-stamping the revision/generation to make it look current -- so
	// its completed disposal restored 0.8 instead of the 0.3 that was
	// actually live right before it muted. hardMute() must detect that its
	// own would-be capture is stale (the revision moved since the last mute,
	// meaning a REAL write happened in between, not another overlapping
	// hardMute()) and recapture instead of reusing it.
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};
	const fake = realMixer(0.8);

	const actionsA = runtime.createGigTeardownActions({
		readMaster: fake.readMaster,
		readMasterWriteRevision: fake.readMasterWriteRevision,
		setMaster: fake.setMaster,
		disposeEngine: async () => {}
	});
	actionsA.hardMute(); // master 0.8 -> 0; pending capture = 0.8

	// A real, independent write -- not another hardMute() -- moves the
	// master while A's capture is still pending.
	fake.setMaster(0.3);

	const disposeCallsB = [];
	await gigTeardown(fake, disposeCallsB)();

	assert.deepEqual(disposeCallsB, ['dispose']);
	assert.equal(
		fake.readMaster(),
		0.3,
		'the second instance must restore the value live right before IT muted (0.3), not A\'s stale capture (0.8)'
	);
	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});
