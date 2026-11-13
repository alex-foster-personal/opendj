/**
 * #14043: a plan's stem_gain step on the bar's sample: the bus guard takes its timed fields, and the engine's
 * scheduled stem gain sets the named part's gain at the given context time.
 * #14376: a manual stem_gain write cancels that part's waiting step; another part's step stays.
 * #14217: a stem knob the DJ moves while a step waits is not reverted when the step lands.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };
const fx = await loadTypeScriptModule('tests/unit/fixtures/stem-fx-send-entry.ts');
const { ScheduledSteps } = await loadTypeScriptModule('src/lib/rb/scheduled-mixer-write.ts');

function fakeNode() {
	return {
		gain: {
			value: 1,
			calls: [],
			setTargetAtTime(v, t, c) {
				this.calls.push([v, t, c]);
				this.value = v;
			},
			cancelled: [],
			cancelScheduledValues(t) {
				this.cancelled.push(t);
			}
		},
		connect() {},
		disconnect() {}
	};
}

function deck() {
	const ctx = { currentTime: 0, createGain: () => fakeNode() };
	const gains = {};
	const sends = {};
	const procs = {};
	for (const part of ['vocals', 'drums', 'bass', 'other']) {
		procs[part] = { connect() {}, disconnect() {}, dispose: async () => {} };
		const bus = fx.createStemPartBus(ctx, procs[part]);
		gains[part] = bus.gain;
		sends[part] = bus.send;
	}
	const p = new fx.AlignedStemDeckProcessor(ctx, procs, gains, sends, 0, 'demucs4', {});
	const st = {
		stems: {
			status: 'ready',
			layout: 'demucs4',
			available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
			controls: fx.createDefaultStemControls(),
			error: null
		}
	};
	const deps = {
		requireLoaded: () => ({ st, rt: { processor: p } }),
		getChannel: () => ({}),
		heldStems: () => null,
		engageHeldStems: () => {}
	};
	return { gains, st, deps };
}

test('a timed stem_gain step with at_context_sec and step_id passes the bus guard', () => {
	/** [if] stem_gain carries at_context_sec and step_id [then] the guard keeps both, [else stop]. */
	const parsed = fx._parseCommand({
		type: 'stem_gain',
		deck: 1,
		stem: 'drums',
		value: 0,
		at_context_sec: 12.5,
		step_id: 's1'
	});
	assert.deepEqual(parsed, {
		type: 'stem_gain',
		deck: 1,
		stem: 'drums',
		value: 0,
		at_context_sec: 12.5,
		step_id: 's1'
	});
	assert.throws(
		() =>
			fx._parseCommand({
				type: 'stem_gain',
				deck: 1,
				stem: 'drums',
				value: 0,
				at_context_sec: 12.5
			}),
		/needs a step_id/
	);
});

test('the scheduled stem step sets the named part gain at the given context time', () => {
	/** [if] a drums gain step is scheduled at 3.25 s [then] the drums part ramps at 3.25 and the mirror waits for landing, [else stop]. */
	const { gains, st, deps } = deck();
	const land = fx.scheduleStemGain(1, 'drums', 0, 3.25, deps);
	assert.deepEqual(gains.drums.gain.calls.at(-1), [0, 3.25, 0.01]);
	assert.equal(st.stems.controls.drums.gain, 0.5, 'mirror unchanged until the step lands');
	land();
	assert.equal(st.stems.controls.drums.gain, 0);
});

function scheduledDeck() {
	const d = deck();
	const timers = [];
	const clock = { now: 0 };
	const steps = new ScheduledSteps(
		() => clock.now,
		(callback) => {
			const handle = { callback, cleared: false };
			timers.push(handle);
			return handle;
		},
		(handle) => (handle.cleared = true)
	);
	d.deps.cancelStemStep = (deckId, stem) => steps.cancelPlain(`stem:${deckId}:${stem}`, null);
	const schedule = (id, stem, value, at) => {
		let land = null;
		steps.schedule(
			id,
			`stem:1:${stem}`,
			at,
			(t) => (land = fx.scheduleStemGain(1, stem, value, t, d.deps)),
			() => land?.()
		);
	};
	const fire = () =>
		(clock.now = 1e9) &&
		timers.filter((t) => !t.cleared).forEach((t) => ((t.cleared = true), t.callback()));
	return { ...d, steps, schedule, fire };
}

test('a manual stem_gain write cancels that part pending step so it does not land', () => {
	/** [if] the drums gain is moved by hand while a drums step waits [then] the step is cancelled and never lands, [else stop]. */
	const { gains, st, deps, steps, schedule, fire } = scheduledDeck();
	schedule('s1', 'drums', 0, 0.5);
	fx.applyStemControl(1, 'drums', 'gain', 0.8, deps);
	assert.equal(steps.state('s1'), 'cancelled');
	assert.ok(gains.drums.gain.cancelled.length > 0, 'the waiting drums automation is dropped');
	fire();
	assert.equal(st.stems.controls.drums.gain, 0.8, 'the manual move wins');
});

test('a manual stem_gain write on another part leaves the pending step in place', () => {
	/** [if] the bass gain is moved by hand while a drums step waits [then] the drums step still waits and lands, [else stop]. */
	const { gains, st, deps, steps, schedule, fire } = scheduledDeck();
	schedule('s1', 'drums', 0, 0.5);
	fx.applyStemControl(1, 'bass', 'gain', 0.8, deps);
	assert.equal(steps.state('s1'), 'waiting');
	assert.equal(gains.drums.gain.cancelled.length, 0);
	fire();
	assert.equal(st.stems.controls.drums.gain, 0);
});

/** An AudioParam that keeps its automation events, so a test can read the value the part plays at a context time. */
function timelineNode() {
	return {
		gain: {
			value: 1,
			events: [],
			setTargetAtTime(v, t) {
				const i = this.events.findIndex((e) => e.t > t);
				this.events.splice(i < 0 ? this.events.length : i, 0, { v, t });
			},
			cancelScheduledValues(t) {
				this.events = this.events.filter((e) => e.t < t);
			},
			at(t) {
				return this.events.filter((e) => e.t <= t).at(-1)?.v ?? this.value;
			}
		},
		connect() {},
		disconnect() {}
	};
}

function timelineDeck() {
	const ctx = { currentTime: 0, createGain: () => timelineNode() };
	const gains = {};
	const procs = {};
	for (const part of ['vocals', 'drums', 'bass', 'other']) {
		procs[part] = { connect() {}, disconnect() {}, dispose: async () => {} };
		const bus = fx.createStemPartBus(ctx, procs[part]);
		gains[part] = bus.gain;
	}
	const sends = Object.fromEntries(Object.keys(gains).map((part) => [part, timelineNode()]));
	const p = new fx.AlignedStemDeckProcessor(ctx, procs, gains, sends, 0, 'demucs4', {});
	const st = {
		stems: {
			status: 'ready',
			layout: 'demucs4',
			available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
			controls: fx.createDefaultStemControls(),
			error: null
		}
	};
	const deps = {
		requireLoaded: () => ({ st, rt: { processor: p } }),
		getChannel: () => ({}),
		heldStems: () => null,
		engageHeldStems: () => {}
	};
	const heard = (part, t) => gains[part].gain.at(t);
	const expected = (part) => fx.stemPartGains(st.stems.controls, 'demucs4')[part];
	const sent = (part, t) => sends[part].gain.at(t);
	return { ctx, st, deps, heard, expected, sent };
}

test('a stem knob the DJ moves while a plan stem step waits is not reverted when the step lands', () => {
	/** [if] a drums step waits for the bar and the DJ moves vocal, mutes bass and sends other to FX [then] at the bar only drums change, [else stop]. */
	const { ctx, st, deps, heard, expected, sent } = timelineDeck();
	ctx.currentTime = 1;
	const land = fx.scheduleStemGain(1, 'drums', 0, 4, deps);
	ctx.currentTime = 2;
	fx.applyStemControl(1, 'vocal', 'gain', 0.9, deps);
	fx.applyStemControl(1, 'bass', 'muted', true, deps);
	fx.applyStemControl(1, 'other', 'fx_send', 0.6, deps);
	ctx.currentTime = 4;
	land();
	assert.equal(st.stems.controls.vocal.gain, 0.9);
	assert.equal(st.stems.controls.bass.muted, true);
	assert.equal(st.stems.controls.drums.gain, 0);
	for (const part of ['vocals', 'drums', 'bass', 'other']) {
		assert.equal(
			heard(part, 5),
			expected(part),
			`${part} plays the DJ's value plus the step after the bar`
		);
	}
	assert.equal(heard('bass', 5), 0, 'the muted bass stays muted');
	assert.equal(sent('other', 5), 0.6, 'the FX send the DJ set stays');
});

test('a manual move on the stem a plan step targets wins over the waiting step', () => {
	/** [if] the DJ moves drums while a drums step waits [then] the DJ's drums value survives the bar, [else stop]. */
	const { ctx, st, deps, heard, expected } = timelineDeck();
	ctx.currentTime = 1;
	const land = fx.scheduleStemGain(1, 'drums', 0, 4, deps);
	ctx.currentTime = 2;
	fx.applyStemControl(1, 'drums', 'gain', 0.8, deps);
	ctx.currentTime = 4;
	land();
	assert.equal(st.stems.controls.drums.gain, 0.8, 'the mirror keeps the DJ value');
	assert.equal(heard('drums', 5), expected('drums'), 'drums play the DJ value after the bar');
	assert.notEqual(heard('drums', 5), 0);
});
