import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// R8 (Wed 7 Oct 2026): hold Ctrl alone and every channel with stems goes to
// STEM; release, blur or a hidden tab puts each channel back to ITS OWN prior
// mode. Ctrl+key or Ctrl+click before the delay never flashes the mode.

const { createStemHold, createHeldKeys, STEM_HOLD_CFG } = await loadTypeScriptModule('src/lib/rb/stem-hold.ts');
const SRC = fileURLToPath(new URL('../../src', import.meta.url));
/** Every refused restore any rig reports, in order. */
const refusedReports = [];
/** Every enable that threw or rejected, in order. */
const enableFailures = [];

function rig({ ready = { 1: true, 2: true, 3: true, 4: false }, mode = { 1: false, 2: true, 3: false, 4: false } } = {}) {
	const state = { ...mode };
	const calls = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: (d, cause) => refusedReports.push([d, cause]),
		reportEnableFailed: (d, cause) => enableFailures.push([d, cause]),
		decks: [1, 2, 3, 4],
		stemsReady: (d) => ready[d],
		stemEqMode: (d) => state[d],
		setStemEqMode: (d, enabled) => {
			calls.push([d, enabled]);
			state[d] = enabled;
			return true;
		},
		setTimer: (fn, ms) => {
			assert.equal(ms, STEM_HOLD_CFG.engageAfterMs);
			pending = fn;
			return 1;
		},
		clearTimer: () => {
			pending = null;
		}
	});
	const elapse = () => {
		const fn = pending;
		pending = null;
		fn?.();
	};
	return { hold, state, calls, elapse };
}

test('R8: hold then release restores each channel to its own prior mode', () => {
	const { hold, state, calls, elapse } = rig();
	hold.ctrlDown();
	assert.deepEqual(calls, [], 'the mode engaged before the hold delay');
	elapse();
	assert.ok(hold.engaged);
	assert.deepEqual(state, { 1: true, 2: true, 3: true, 4: false }, 'a deck with stems did not switch, or one without did');
	hold.release();
	assert.deepEqual(state, { 1: false, 2: true, 3: false, 4: false }, 'a channel did not return to its OWN prior mode');
	assert.deepEqual(calls, [[1, true], [3, true], [1, false], [3, false]], 'a deck already in STEM or with no stems was touched');
});

test('R8 mutation control: restoring every deck to off would turn deck 2 STEM off', () => {
	// Deck 2 was in STEM before the hold. A restore-all-to-off implementation
	// sets it false, which the assertion above rejects; prove the fixture has
	// such a deck so that test can bite.
	const { hold, state, elapse } = rig();
	hold.ctrlDown();
	elapse();
	hold.release();
	assert.equal(state[2], true);
	assert.ok(!hold.switchedOn.includes(2));
});

test('R8: blur while held restores, exactly as a release', () => {
	const { hold, state, elapse } = rig();
	hold.ctrlDown();
	elapse();
	hold.release(); // the installer routes blur and visibilitychange:hidden here
	assert.ok(!hold.engaged);
	assert.deepEqual(state, { 1: false, 2: true, 3: false, 4: false });
});

test('R8: Ctrl+Z before the delay never flashes the mode', () => {
	const { hold, calls, elapse } = rig();
	hold.ctrlDown();
	hold.release(); // the Z keydown
	elapse();
	assert.deepEqual(calls, [], 'Ctrl+Z switched a channel');
	assert.ok(!hold.engaged);
});

test('R8: a repeat Ctrl keydown while engaged does not re-snapshot', () => {
	const { hold, state, elapse } = rig();
	hold.ctrlDown();
	elapse();
	hold.ctrlDown();
	hold.release();
	assert.deepEqual(state, { 1: false, 2: true, 3: false, 4: false });
});

test('R8 wiring: installer uses the STEM button command, guards typing, and restores on blur and hidden', () => {
	const src = readFileSync(`${SRC}/lib/rb/stem-hold-hotkeys.ts`, 'utf8');
	assert.match(src, /setStemEqMode: \(deck, enabled\) => sendStemEqMode\(deck, enabled\)/);
	assert.doesNotMatch(src, /from '\$lib\/rb\/performance-ipc/, 'the command sender is injected (performance-ipc fan-in ratchet)');
	assert.match(src, /isTextEntryTarget\(e\.target\)/);
	assert.match(src, /addEventListener\('blur'/);
	assert.match(src, /visibilityState !== 'hidden'\) return;/);
	assert.match(src, /addEventListener\('pointerdown'/);
	const page = readFileSync(`${SRC}/routes/performance/+page.svelte`, 'utf8');
	assert.match(
		page,
		/installStemHoldHotkey\(\s*async \(deck, enabled\) =>\s*\(await runPerformanceCommandFromUi\(\{ type: 'stem_eq_mode', deck, enabled \}\)\)\.ok\s*\)/,
		'the sender must report whether the dispatcher accepted the enable'
	);
	const reg = readFileSync(`${SRC}/lib/components/rb/hotkeys/hotkeys-registry.ts`, 'utf8');
	assert.match(reg, /id: 'mixer-stem-hold'/);
});

test('R8: a rejected enable is never restored to off (a preset lock owns that mode)', () => {
	const state = { 1: false, 2: false };
	const calls = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: (d, cause) => refusedReports.push([d, cause]),
		reportEnableFailed: (d, cause) => enableFailures.push([d, cause]),
		decks: [1, 2],
		stemsReady: () => true,
		stemEqMode: (d) => state[d],
		setStemEqMode: (d, enabled) => {
			calls.push([d, enabled]);
			if (d === 2 && enabled) return false; // the lock rejects deck 2's enable
			state[d] = enabled;
			return true;
		},
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	hold.ctrlDown();
	pending();
	assert.deepEqual(hold.switchedOn, [1], 'a deck whose enable was rejected joined the restore set');
	state[2] = true; // the preset, once its lock lifts, puts deck 2 in STEM
	hold.release();
	assert.equal(state[2], true, 'the release overwrote the preset-set mode');
	assert.deepEqual(calls, [[1, true], [2, true], [1, false]]);
});

// The engine applies each command in the order it was sent; the mixer state the
// hold reads catches up only when the answer comes back. Answers are released
// by hand so a test can interleave two holds.
function laggedRig() {
	const engine = { 1: false };
	const mixer = { 1: false };
	const answers = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: (d, cause) => refusedReports.push([d, cause]),
		reportEnableFailed: (d, cause) => enableFailures.push([d, cause]),
		decks: [1],
		stemsReady: () => true,
		stemEqMode: (d) => mixer[d],
		setStemEqMode: (d, enabled) => {
			engine[d] = enabled;
			return new Promise((resolve) => answers.push(() => ((mixer[d] = engine[d]), resolve(true))));
		},
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	const press = () => {
		hold.ctrlDown();
		pending();
	};
	const answer = async () => {
		answers.shift()();
		await new Promise((resolve) => setImmediate(resolve));
	};
	const drain = async () => {
		while (answers.length > 0) await answer();
	};
	/** The DJ's own STEM button press, through the same ordered command path. */
	const click = (enabled) => {
		engine[1] = enabled;
		answers.push(() => (mixer[1] = engine[1]));
	};
	return { hold, engine, mixer, press, answer, drain, click };
}

test('R8: a stale enable from an ended hold never turns off a newer hold', async () => {
	const { hold, engine, press, drain } = laggedRig();
	press(); // hold A sends its enable
	hold.release(); // released before A's enable was confirmed
	press(); // hold B: the mixer still reads off, so B sends its own enable
	await drain(); // A's enable, A's restore, B's enable
	assert.equal(engine[1], true, "A's late cleanup switched the deck off under hold B");
	assert.deepEqual(hold.switchedOn, [1], 'hold B must own the deck exactly once');
	hold.release();
	assert.equal(engine[1], false, 'releasing B must restore the pre-hold mode');
});

test('R8: a hold engaged while the last restore is in flight still switches the deck on', async () => {
	const { hold, engine, press, answer } = laggedRig();
	press();
	await answer(); // A confirmed while held
	hold.release(); // restore sent; the mixer still reads STEM
	press(); // hold B must not read the in-flight deck as "already in STEM"
	assert.equal(engine[1], true, 'hold B skipped a deck whose restore was still in flight');
	await answer(); // the restore's answer
	await answer(); // B's enable answer
	assert.deepEqual(hold.switchedOn, [1]);
	hold.release();
	assert.equal(engine[1], false);
});

test('R8: an enable still unanswered at release is switched back off by the release itself', async () => {
	const { hold, engine, mixer, press, drain } = laggedRig();
	press();
	hold.release(); // Ctrl up before the enable was confirmed
	assert.equal(engine[1], false, 'the release did not send the restore for an in-flight enable');
	await drain();
	assert.equal(mixer[1], false, 'a late enable left the deck stuck in STEM');
	assert.deepEqual(hold.switchedOn, [], 'an ended hold adopted its own late answer');
	// Control: an enable confirmed while still held waits for the release.
	press();
	await drain();
	assert.equal(engine[1], true, 'a confirmed enable was undone while Ctrl was still held');
	assert.deepEqual(hold.switchedOn, [1]);
	hold.release();
	assert.equal(engine[1], false);
});

test("R8: a late enable answer never overrides the DJ's command sent after the hold ended", async () => {
	const { hold, engine, mixer, press, drain, click } = laggedRig();
	press(); // hold sends its enable
	hold.release(); // a pointer press ends the hold before the enable is answered
	click(true); // ...and the click that follows presses STEM on
	await drain();
	assert.equal(engine[1], true, "the hold's late cleanup turned off the DJ's own STEM command");
	assert.equal(mixer[1], true);
	// Control: the DJ turning it off again is also left alone.
	click(false);
	await drain();
	assert.equal(engine[1], false);
});

// Sol P1, PR #6834: a restore the dispatcher refuses (a preset claim that
// landed between the enable and the release) is neither silent nor forgotten.
function refusingRig() {
	const state = { 1: false, 2: false };
	const calls = [];
	let refuse = false;
	let throwInstead = false;
	let pending = null;
	const reports = [];
	const ready = { 1: true, 2: true };
	const hold = createStemHold({
		reportRestoreRefused: (d, cause) => reports.push([d, cause]),
		reportEnableFailed: (d, cause) => enableFailures.push([d, cause]),
		decks: [1, 2],
		stemsReady: (d) => ready[d],
		stemEqMode: (d) => state[d],
		setStemEqMode: (d, enabled) => {
			calls.push([d, enabled]);
			if (!enabled && throwInstead) return Promise.reject(new Error('dead session'));
			if (!enabled && refuse) return Promise.resolve(false);
			state[d] = enabled;
			return Promise.resolve(true);
		},
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	const tick = () => new Promise((resolve) => setImmediate(resolve));
	const press = async () => {
		hold.ctrlDown();
		pending();
		await tick();
	};
	return {
		hold,
		state,
		calls,
		reports,
		ready,
		press,
		tick,
		setRefuse: (v) => (refuse = v),
		setThrow: (v) => (throwInstead = v)
	};
}

test('R8: a refused restore is reported and kept, and the next hold retries it', async () => {
	const r = refusingRig();
	await r.press();
	assert.deepEqual(r.hold.switchedOn, [1, 2]);
	r.setRefuse(true); // the preset claim lands while Ctrl is still held
	r.hold.release();
	await r.tick();
	assert.deepEqual(r.state, { 1: true, 2: true }, 'the refused restores changed the mode anyway');
	assert.deepEqual(r.reports.map(([d]) => d), [1, 2], 'a refused restore went unreported');
	assert.deepEqual(r.hold.unrestored, [1, 2], 'a refused restore was forgotten');
	// The claim clears; the DJ turns deck 2 off by hand meanwhile.
	r.setRefuse(false);
	r.state[2] = false;
	await r.press(); // the next hold adopts deck 1 (still STEM) and enables deck 2
	assert.deepEqual(r.hold.unrestored, []);
	assert.deepEqual(r.hold.switchedOn, [1, 2]);
	r.hold.release();
	await r.tick();
	assert.deepEqual(r.state, { 1: false, 2: false }, 'the owed restore was not retried');
	assert.equal(r.reports.length, 2, 'a successful retry was reported as refused');
});

test('R8: a restore that throws is reported and kept like a refusal', async () => {
	const r = refusingRig();
	await r.press();
	r.setThrow(true);
	r.hold.release();
	await r.tick();
	assert.equal(r.reports.length, 2);
	assert.match(String(r.reports[0][1]), /dead session/);
	assert.deepEqual(r.hold.unrestored, [1, 2]);
});

test('R8 control: a confirmed restore reports nothing and owes nothing', async () => {
	const r = refusingRig();
	await r.press();
	r.hold.release();
	await r.tick();
	assert.deepEqual(r.reports, []);
	assert.deepEqual(r.hold.unrestored, []);
	assert.deepEqual(r.state, { 1: false, 2: false });
	assert.deepEqual(refusedReports, [], 'a sync rig reported a refusal');
});

test('R8 control: an owed deck that no longer reads STEM is dropped, not re-adopted', async () => {
	const r = refusingRig();
	await r.press();
	r.setRefuse(true);
	r.hold.release();
	await r.tick();
	r.setRefuse(false);
	r.state[2] = false; // settled elsewhere (the DJ, or the preset)
	r.ready[2] = false; // and its stems went away, so no new enable either
	await r.press();
	assert.deepEqual(r.hold.switchedOn, [1], 'a deck no longer in STEM was adopted for a restore');
	assert.deepEqual(r.hold.unrestored, []);
});

// Sol P1, PR #6834 (second round): an enable still unanswered at the release
// is restored at once (ordering, test above), but a refused restore is owed
// only if that enable then ran. Otherwise the hold never switched the deck on.
function unansweredRig() {
	const state = { 1: false };
	const reports = [];
	const enables = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: (d, cause) => reports.push([d, cause]),
		reportEnableFailed: (d, cause) => enableFailures.push([d, cause]),
		decks: [1],
		stemsReady: () => true,
		stemEqMode: (d) => state[d],
		setStemEqMode: (d, enabled) => {
			if (enabled) return new Promise((resolve) => enables.push(resolve));
			return Promise.resolve(false); // every restore here is refused
		},
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	const tick = () => new Promise((resolve) => setImmediate(resolve));
	const press = () => {
		hold.ctrlDown();
		pending();
	};
	return { hold, state, reports, enables, press, tick };
}

test('R8: a refused restore for an enable that then failed owes nothing', async () => {
	const r = unansweredRig();
	r.press();
	r.hold.release(); // the enable is still unanswered
	await r.tick();
	assert.deepEqual(r.reports, [], 'reported before the enable answered');
	r.enables[0](false); // the enable did not run
	await r.tick();
	assert.deepEqual(r.reports, [], 'a deck this hold never switched on was reported as left in STEM');
	assert.deepEqual(r.hold.unrestored, [], 'a deck this hold never switched on is owed a restore');
	// Someone else puts the deck in STEM; the next hold must leave it alone.
	r.state[1] = true;
	r.press();
	assert.deepEqual(r.hold.switchedOn, [], 'the next hold adopted a STEM mode it never set');
});

test('R8 control: a refused restore for an enable that then ran is owed', async () => {
	const r = unansweredRig();
	r.press();
	r.hold.release();
	r.enables[0](true);
	await r.tick();
	assert.deepEqual(r.reports.map(([d]) => d), [1], 'a refused restore of a deck this hold switched on went unreported');
	assert.deepEqual(r.hold.unrestored, [1]);
});

// Sol P1, PR #6834 (third round): an enable that rejects or throws is an IPC
// or engine error, not a refusal: it is reported, never swallowed, and one
// deck's error does not stop the others from engaging.
test('R8: a rejected or throwing enable is reported, and the other decks still engage', async () => {
	const state = { 1: false, 2: false, 3: false };
	const failures = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: () => assert.fail('no restore was refused'),
		reportEnableFailed: (d, cause) => failures.push([d, String(cause)]),
		decks: [1, 2, 3],
		stemsReady: () => true,
		stemEqMode: (d) => state[d],
		setStemEqMode: (d, enabled) => {
			if (enabled && d === 1) return Promise.reject(new Error('ipc down'));
			if (enabled && d === 2) throw new Error('engine threw');
			state[d] = enabled;
			return Promise.resolve(true);
		},
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	hold.ctrlDown();
	pending();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(failures, [[2, 'Error: engine threw'], [1, 'Error: ipc down']], 'a failed enable was swallowed');
	assert.deepEqual(hold.switchedOn, [3], 'a deck after the one that threw did not engage, or a failed one was adopted');
	hold.release();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(state, { 1: false, 2: false, 3: false });
});

test('R8 control: a refused enable (resolves false) is not reported as a failure', async () => {
	const failures = [];
	let pending = null;
	const hold = createStemHold({
		reportRestoreRefused: () => {},
		reportEnableFailed: (d, cause) => failures.push([d, cause]),
		decks: [1],
		stemsReady: () => true,
		stemEqMode: () => false,
		setStemEqMode: () => Promise.resolve(false),
		setTimer: (fn) => ((pending = fn), 1),
		clearTimer: () => (pending = null)
	});
	hold.ctrlDown();
	pending();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(failures, [], 'a refusal the command path already toasted was reported twice');
	assert.deepEqual(hold.switchedOn, []);
	assert.deepEqual(enableFailures, [], 'a shared rig reported an enable failure');
});

test('R8 wiring: a failed enable is reported by the installer', () => {
	const src = readFileSync(`${SRC}/lib/rb/stem-hold-hotkeys.ts`, 'utf8');
	assert.match(src, /reportEnableFailed: \(deck, cause\) =>\s*console\.error\(`\[stem-hold\] deck \$\{deck\} not switched to STEM/);
});

test('R8 wiring: a refused restore is reported by the installer', () => {
	const src = readFileSync(`${SRC}/lib/rb/stem-hold-hotkeys.ts`, 'utf8');
	assert.match(src, /reportRestoreRefused: \(deck, cause\) =>\s*console\.error\(`\[stem-hold\] deck \$\{deck\} stayed in STEM/);
});

// Sol P1, PR #6834 (fourth round): the Control event's modifier flags cannot
// see a plain key already held, so the installer tracks held keys itself.
test('R8: a plain key held before Control blocks the hold until it is released', () => {
	const keys = createHeldKeys();
	assert.equal(keys.othersHeld, false, 'nothing held reads as held');
	keys.down('KeyZ');
	assert.equal(keys.othersHeld, true, 'holding Z did not register');
	keys.down('KeyZ'); // auto-repeat
	keys.up('KeyZ');
	assert.equal(keys.othersHeld, false, 'a repeat counted twice kept Z held after its keyup');
	keys.down('KeyZ');
	keys.down('KeyX');
	keys.up('KeyZ');
	assert.equal(keys.othersHeld, true, 'releasing one of two held keys cleared both');
	keys.clear(); // blur or hidden tab: their keyups never arrive
	assert.equal(keys.othersHeld, false);
});

test('R8 wiring: the installer arms only with no other key held, and forgets keys on blur and hidden', () => {
	const src = readFileSync(`${SRC}/lib/rb/stem-hold-hotkeys.ts`, 'utf8');
	assert.match(src, /if \(e\.repeat \|\| heldKeys\.othersHeld \|\| /, 'Ctrl arms while another key is held');
	assert.match(src, /if \(e\.key !== 'Control'\) heldKeys\.down\(e\.code\)/, 'a plain keydown is not tracked by code');
	assert.match(src, /else heldKeys\.up\(e\.code\)/, 'a plain keyup is not untracked');
	assert.match(src, /const onBlur = \(\): void => \{\s*heldKeys\.clear\(\);/, 'blur keeps keys whose keyup was lost');
	assert.match(src, /visibilityState !== 'hidden'\) return;\s*heldKeys\.clear\(\);/, 'a hidden tab keeps keys whose keyup was lost');
});

test('R8: a missing restore count fails loudly rather than reading as a clean end', () => {
	const src = readFileSync(`${SRC}/lib/rb/stem-hold.ts`, 'utf8');
	assert.doesNotMatch(src, /restoring\.get\(deck\) \?\? 1/, 'a missing count still defaults to a clean final cleanup');
	assert.match(src, /if \(held === undefined\) throw new Error\(`stem-hold: deck \$\{String\(deck\)\} restore count missing`\)/);
});

// Sol P1 on PR #6834: while a library preview sounds, the preview stopper
// consumes Space with stopImmediatePropagation, so a hold listener installed
// after it never sees that Space: the pending hold is not cancelled and the
// held-key set never learns Space went up. The page installs the hold first.
test('R8 order: the Ctrl-hold listener is installed before the preview Space stopper', () => {
	const page = readFileSync(`${SRC}/routes/performance/+page.svelte`, 'utf8');
	const hold = page.indexOf('installStemHoldHotkey(');
	const stopper = page.indexOf('installPreviewSpaceStop({');
	assert.ok(hold > 0 && stopper > 0, 'both installers must be wired on /performance');
	assert.ok(hold < stopper, 'if the hold listener follows the preview stopper then Ctrl then Space still engages STEM - broken');
});

test('R8 order: a window capture listener registered before the real preview stopper still sees the consumed Space, one after it does not', async () => {
	const { installPreviewSpaceStop } = await loadTypeScriptModule('src/lib/player/preview-space-stop.ts');
	const hadWindow = 'window' in globalThis;
	const prevWindow = globalThis.window;
	globalThis.window = new EventTarget();
	try {
		const seen = { before: [], after: [] };
		const record = (bucket) => (e) => seen[bucket].push(`${e.type}:${e.code}`);
		for (const type of ['keydown', 'keyup']) window.addEventListener(type, record('before'), { capture: true });
		let stops = 0;
		const uninstall = installPreviewSpaceStop({ isPreviewPlaying: () => stops === 0, stopPreview: () => (stops += 1) });
		for (const type of ['keydown', 'keyup']) window.addEventListener(type, record('after'), { capture: true });
		const key = (type) => Object.assign(new Event(type, { cancelable: true }), { code: 'Space', key: ' ' });
		const down = key('keydown');
		window.dispatchEvent(down);
		window.dispatchEvent(key('keyup'));
		uninstall();
		assert.equal(stops, 1, 'the real stopper must have consumed this Space');
		assert.ok(down.defaultPrevented, 'the real stopper must have consumed this Space');
		assert.deepEqual(seen.before, ['keydown:Space', 'keyup:Space'], 'a listener ahead of the stopper must see both halves of the press');
		assert.deepEqual(seen.after, [], 'control: a listener behind the stopper is starved, which is why order matters');
	} finally {
		if (hadWindow) globalThis.window = prevWindow;
		else delete globalThis.window;
	}
});
