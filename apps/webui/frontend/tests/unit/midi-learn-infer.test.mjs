// MIDI learn control-kind inference (issue #10068).
// if a 0..127 fader sweep isn't absolute then broken
// if alternating 0x01/0x7F isn't relative-twos then broken
// if a single CC entry isn't ambiguous then broken
// if LearnWindow cc doesn't use a 600 ms timer then broken

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const m = await loadTypeScriptModule('src/lib/rb/midi/learn-infer.ts');

function cc(ts, ch, id, value, deviceId = 'dev-a') {
	const status = 0xb0 | (ch - 1);
	return {
		ts,
		deviceId,
		deviceName: 'Test',
		status,
		data1: id,
		data2: value,
		decoded: { ch, kind: 'cc', id },
		mapped: false,
		note: 'unmapped'
	};
}

function note(ts, ch, id, velocity = 127, deviceId = 'dev-a') {
	const status = 0x90 | (ch - 1);
	return {
		ts,
		deviceId,
		deviceName: 'Test',
		status,
		data1: id,
		data2: velocity,
		decoded: { ch, kind: 'note', id },
		mapped: false,
		note: 'unmapped'
	};
}

function sweep0to127() {
	const out = [];
	for (let v = 0; v <= 127; v += 4) {
		out.push(cc(v, 1, 7, v));
	}
	out.push(cc(128, 1, 7, 127));
	return out;
}

test('fader sweep 0..127 is absolute', () => {
	const r = m.inferControlKind(sweep0to127());
	assert.equal(r.kind, 'absolute');
});

test('alternating 0x01/0x7F is relative-twos', () => {
	const entries = [];
	for (let i = 0; i < 8; i++) {
		entries.push(cc(i, 1, 16, i % 2 === 0 ? 0x01 : 0x7f));
	}
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'relative-twos');
});

test('alternating 0x41/0x3F is relative-centred', () => {
	const entries = [];
	for (let i = 0; i < 8; i++) {
		entries.push(cc(i, 1, 21, i % 2 === 0 ? 0x41 : 0x3f));
	}
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'relative-centred');
});

test('CC 1 / CC 33 pairs are absolute-14bit', () => {
	const entries = [];
	let ts = 0;
	for (let pair = 0; pair < 6; pair++) {
		entries.push(cc(ts++, 1, 1, 64 + pair));
		entries.push(cc(ts++, 1, 33, pair * 2));
	}
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'absolute-14bit');
	assert.equal(r.lsbOffset, 32);
	assert.equal(r.source.id, 1);
});

test('LSB-first window still resolves MSB 1 as absolute-14bit', () => {
	const entries = [];
	let ts = 0;
	for (let pair = 0; pair < 6; pair++) {
		entries.push(cc(ts++, 1, 33, pair));
		entries.push(cc(ts++, 1, 1, 64 + pair));
	}
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'absolute-14bit');
	assert.equal(r.source.id, 1);
});

test('CC only 0x7F and 0x00 is button-cc', () => {
	const entries = [];
	for (let i = 0; i < 6; i++) {
		entries.push(cc(i, 1, 99, i % 2 === 0 ? 0x7f : 0x00));
	}
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'button-cc');
});

test('single CC entry is ambiguous', () => {
	const r = m.inferControlKind([cc(0, 1, 7, 64)]);
	assert.equal(r.kind, 'ambiguous');
	assert.ok(r.reason.length > 0);
});

test('empty or undecoded window is ambiguous', () => {
	assert.equal(m.inferControlKind([]).kind, 'ambiguous');
	const undecoded = [
		{
			ts: 0,
			deviceId: 'dev-a',
			deviceName: 'T',
			status: 0xc0,
			data1: 0,
			data2: 0,
			decoded: null,
			mapped: false,
			note: 'undecoded'
		}
	];
	assert.equal(m.inferControlKind(undecoded).kind, 'ambiguous');
});

test('note window is button-note', () => {
	const r = m.inferControlKind([note(0, 1, 60)]);
	assert.equal(r.kind, 'button-note');
});

test('two controls with 4+ entries each is ambiguous', () => {
	const entries = [];
	for (let i = 0; i < 4; i++) entries.push(cc(i, 1, 7, 10 + i));
	for (let i = 0; i < 4; i++) entries.push(cc(10 + i, 1, 8, 20 + i));
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'ambiguous');
	assert.match(r.reason, /CC 7/);
	assert.match(r.reason, /CC 8/);
});

test('newest-first order matches sorted inference for fader sweep', () => {
	const fwd = sweep0to127();
	const rev = fwd.slice().reverse();
	assert.equal(m.inferControlKind(rev).kind, m.inferControlKind(fwd).kind);
});

test('monotone 0x01 x6 is relative-twos', () => {
	const entries = [];
	for (let i = 0; i < 6; i++) entries.push(cc(i, 1, 16, 0x01));
	const r = m.inferControlKind(entries);
	assert.equal(r.kind, 'relative-twos');
});

test('fader creeping 1,2,3,4,5 is ambiguous (dual pattern)', () => {
	const r = m.inferControlKind([1, 2, 3, 4, 5].map((v, i) => cc(i, 1, 7, v)));
	assert.equal(r.kind, 'ambiguous');
});

test('learnedBindingFlags matches the flag table', () => {
	assert.deepEqual(
		m.learnedBindingFlags({ kind: 'absolute', source: { ch: 1, kind: 'cc', id: 1 } }),
		{
			relative: false,
			relativeCentered: false,
			ccButton: false
		}
	);
	assert.deepEqual(
		m.learnedBindingFlags({
			kind: 'absolute-14bit',
			source: { ch: 1, kind: 'cc', id: 1 },
			lsbOffset: 32
		}),
		{ relative: false, relativeCentered: false, ccButton: false }
	);
	assert.deepEqual(
		m.learnedBindingFlags({ kind: 'relative-twos', source: { ch: 1, kind: 'cc', id: 1 } }),
		{
			relative: true,
			relativeCentered: false,
			ccButton: false
		}
	);
	assert.deepEqual(
		m.learnedBindingFlags({ kind: 'relative-centred', source: { ch: 1, kind: 'cc', id: 1 } }),
		{ relative: true, relativeCentered: true, ccButton: false }
	);
	assert.deepEqual(
		m.learnedBindingFlags({ kind: 'button-cc', source: { ch: 1, kind: 'cc', id: 1 } }),
		{
			relative: false,
			relativeCentered: false,
			ccButton: true
		}
	);
	assert.deepEqual(
		m.learnedBindingFlags({ kind: 'button-note', source: { ch: 1, kind: 'note', id: 60 } }),
		{
			relative: false,
			relativeCentered: false,
			ccButton: false
		}
	);
	assert.equal(m.learnedBindingFlags({ kind: 'ambiguous', reason: 'x', source: null }), null);
});

test('buildLearnedBinding sets deck_pitch lsbOffset for 14-bit', () => {
	const r = {
		kind: 'absolute-14bit',
		source: { ch: 1, kind: 'cc', id: 1 },
		lsbOffset: 32
	};
	const b = m.buildLearnedBinding({ action: { type: 'deck_pitch', deck: 1, lsbOffset: null } }, r);
	assert.equal(b.action.lsbOffset, 32);
});

test('buildLearnedBinding clears deck_pitch lsbOffset for absolute', () => {
	const r = { kind: 'absolute', source: { ch: 1, kind: 'cc', id: 1 } };
	const b = m.buildLearnedBinding({ action: { type: 'deck_pitch', deck: 1, lsbOffset: 32 } }, r);
	assert.equal(b.action.lsbOffset, null);
});

test('buildLearnedBinding sets relative for browse_encoder', () => {
	const r = { kind: 'relative-twos', source: { ch: 1, kind: 'cc', id: 16 } };
	const b = m.buildLearnedBinding({ action: { type: 'browse_encoder' } }, r);
	assert.equal(b.relative, true);
});

test('buildLearnedBinding returns null for ambiguous', () => {
	assert.equal(
		m.buildLearnedBinding(
			{ action: { type: 'browse_encoder' } },
			{ kind: 'ambiguous', reason: 'no', source: null }
		),
		null
	);
});

test('buildLearnedBinding does not mutate non-deck_pitch actions', () => {
	const action = { type: 'browse_load', deck: 1 };
	const r = { kind: 'absolute-14bit', source: { ch: 1, kind: 'cc', id: 1 }, lsbOffset: 32 };
	const b = m.buildLearnedBinding({ action }, r);
	assert.equal(b.action, action);
});

// ---- LearnWindow

test('LearnWindow cc starts one 600 ms timer', () => {
	const scheduled = [];
	const timers = {
		setTimeout: (fn, ms) => {
			scheduled.push({ fn, ms });
			return scheduled.length;
		},
		clearTimeout: () => {}
	};
	let done;
	const w = new m.LearnWindow((r) => (done = r), timers);
	w.push(cc(0, 1, 7, 1));
	assert.equal(w.capturing, true);
	assert.equal(scheduled.length, 1);
	assert.equal(scheduled[0].ms, 600);
	w.push(cc(1, 1, 7, 2));
	assert.equal(scheduled.length, 1);
});

test('LearnWindow timer fires inferControlKind on stored entries', () => {
	const scheduled = [];
	const timers = {
		setTimeout: (fn, ms) => {
			scheduled.push(fn);
			return 1;
		},
		clearTimeout: () => {}
	};
	let result;
	const w = new m.LearnWindow((r) => (result = r), timers);
	for (let i = 0; i < 8; i++) w.push(cc(i, 1, 16, i % 2 === 0 ? 0x01 : 0x7f));
	scheduled[0]();
	assert.equal(result.kind, 'relative-twos');
	assert.equal(w.done, true);
});

test('LearnWindow ignores other devices', () => {
	const scheduled = [];
	const timers = {
		setTimeout: (fn) => {
			scheduled.push(fn);
			return 1;
		},
		clearTimeout: () => {}
	};
	const w = new m.LearnWindow(() => {}, timers);
	w.push(cc(0, 1, 7, 1));
	w.push(cc(1, 1, 7, 2, 'dev-b'));
	scheduled[0]();
	assert.equal(w.done, true);
});

test('LearnWindow note binds synchronously without timer', () => {
	const scheduled = [];
	const timers = {
		setTimeout: (fn, ms) => {
			scheduled.push({ fn, ms });
			return 1;
		},
		clearTimeout: () => {}
	};
	let result;
	const w = new m.LearnWindow((r) => (result = r), timers);
	w.push(note(0, 1, 60));
	assert.equal(result.kind, 'button-note');
	assert.equal(scheduled.length, 0);
	assert.equal(w.done, true);
});

test('LearnWindow cancel clears timer and ignores later pushes', () => {
	let cleared = false;
	const timers = {
		setTimeout: () => 1,
		clearTimeout: () => {
			cleared = true;
		}
	};
	const w = new m.LearnWindow(() => {}, timers);
	w.push(cc(0, 1, 7, 1));
	w.cancel();
	assert.equal(cleared, true);
	assert.equal(w.push(cc(2, 1, 7, 3)), w.done);
});
