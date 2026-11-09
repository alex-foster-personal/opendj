/**
 * requirement: SKIN-9565
 *
 * Practice compare scale and blind A/B report (issue #9406, ADR-0137).
 *
 * Regression:
 * - if a 965 x 545 reference against the FLX4 panel does not put both layers at 2.0 px per mm then broken
 * - if blind mode with the same seed gives a different order, or every seed gives the same order, then broken
 * - if the downloaded JSON lacks trial_id, order, rating or choice for an answered trial then broken
 * - if the reference fixture is not under tests/ then broken
 * - if the fixture-path check fails on Windows separators then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { posix, relative, resolve, win32 } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { createPracticeCompareIpc } from '../../src/lib/components/rb/practice/practice-compare-ipc.ts';
import {
	applyCompareCommand,
	blindTrials,
	buildCompareReport,
	compareQuery,
	compareScale,
	mulberry32,
	newCompareSession,
	parseCompareCommand,
	pxPerMm,
	recordAnswer,
	refSource,
	shuffledOrder
} from '../../src/lib/components/rb/practice/practice-compare.ts';

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const FIXTURE = resolve(REPO, 'tests/skins/fixtures/practice-compare-965x545.png');
const VECTORS_PATH = resolve(REPO, 'tests/skins/fixtures/practice-compare-vectors.json');
const PANEL_PATH = resolve(REPO, 'tools/deck-diagrams/devices/ddj-flx4/panel.json');

function topSegment(rel) {
	return rel.split(/[/\\]/).filter(Boolean)[0];
}

function readPngSize(path) {
	const buf = readFileSync(path);
	assert.equal(buf.toString('ascii', 1, 4), 'PNG');
	const w = buf.readUInt32BE(16);
	const h = buf.readUInt32BE(20);
	return { width: w, height: h };
}

test('965x545 fixture path and IHDR size', () => {
	const rel = relative(REPO, FIXTURE);
	const top = topSegment(rel);
	assert.equal(top, 'tests', rel);
	assert.notEqual(top, 'apps');
	assert.notEqual(top, 'data');
	const relWin = win32.relative(
		'C:\\repo',
		'C:\\repo\\tests\\skins\\fixtures\\practice-compare-965x545.png'
	);
	assert.equal(topSegment(relWin), 'tests', relWin);
	const relPosix = posix.relative(
		'/repo',
		'/repo/tests/skins/fixtures/practice-compare-965x545.png'
	);
	assert.equal(topSegment(relPosix), 'tests', relPosix);
	const { width, height } = readPngSize(FIXTURE);
	assert.equal(width, 965);
	assert.equal(height, 545);
});

test('compareScale matches 2.0 px/mm for FLX4 and fixture image', () => {
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	const { width, height } = readPngSize(FIXTURE);
	assert.equal(pxPerMm(965, 482.6), 2);
	const cs = compareScale(panel, { width, height });
	assert.equal(cs.pxPerMm, 2);
	assert.ok(Math.abs(cs.replica.width - 965) <= 1);
	assert.ok(Math.abs(cs.replica.height - 544.8) <= 1);
	assert.deepEqual(cs.reference, { width: 965, height: 545 });
});

test('blind trials: stable seed, varied orders, grid tiles panel', () => {
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	const a = blindTrials(panel, 42);
	const b = blindTrials(panel, 42);
	assert.deepEqual(a, b);
	assert.equal(a.length, 6);
	for (let i = 0; i < 6; i++) {
		assert.equal(a[i].trial_id, `t${i + 1}`);
	}
	const seqs = new Set();
	let sawReplicaFirst = false;
	let sawReferenceFirst = false;
	for (let seed = 1; seed <= 20; seed++) {
		const t = blindTrials(panel, seed);
		const key = t.map((x) => x.order.join(',')).join('|');
		seqs.add(key);
		if (t[0].order[0] === 'replica') sawReplicaFirst = true;
		if (t[0].order[0] === 'reference') sawReferenceFirst = true;
		for (const trial of t) {
			const sorted = [...trial.order].sort().join(',');
			assert.equal(sorted, 'reference,replica');
		}
	}
	assert.ok(seqs.size >= 2);
	assert.ok(sawReplicaFirst);
	assert.ok(sawReferenceFirst);
	let rowW = 0;
	for (let c = 0; c < 3; c++) {
		rowW += a[c].region.w;
	}
	assert.ok(Math.abs(rowW - panel.w) < 0.01);
});

test('recordAnswer and buildCompareReport', () => {
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	let trials = blindTrials(panel, 42);
	trials = recordAnswer(trials, 't1', 'A', 4);
	assert.equal(trials[0].choice, trials[0].order[0]);
	trials = recordAnswer(trials, 't2', 'B', 2);
	assert.equal(trials[1].choice, trials[1].order[1]);
	const report = buildCompareReport({
		device: 'ddj-flx4',
		ref: 'https://example.com/x.jpg',
		seed: 42,
		pxPerMm: 2,
		trials,
		recordedAt: '2026-11-06T12:00:00.000Z'
	});
	assert.equal(report.format, 'opendj-practice-compare/1');
	assert.equal(report.seed, 42);
	assert.equal(report.trials.length, 2);
	for (const tr of report.trials) {
		assert.ok(tr.trial_id);
		assert.ok(Array.isArray(tr.order));
		assert.ok(typeof tr.rating === 'number');
		assert.ok(tr.choice === 'replica' || tr.choice === 'reference');
	}
	const roundTrip = JSON.parse(JSON.stringify(report));
	assert.deepEqual(roundTrip, report);
	const fresh = blindTrials(panel, 7);
	assert.throws(() => recordAnswer(fresh, 't1', 'A', 0));
	assert.throws(() => recordAnswer(fresh, 't1', 'A', 6));
	assert.throws(() => recordAnswer(fresh, 't1', 'A', 2.5));
	assert.throws(() => recordAnswer(fresh, 'tx', 'A', 3));
});

test('refSource', () => {
	assert.deepEqual(refSource('https://x/y.jpg'), { kind: 'url', url: 'https://x/y.jpg' });
	const fsu = refSource('file:///tmp/a.png');
	assert.equal(fsu.kind, 'file');
	assert.equal(fsu.fileName, 'a.png');
	assert.deepEqual(refSource(null), { kind: 'none' });
});

test('shuffledOrder permutes with mulberry stream', () => {
	const r = mulberry32(1);
	const o = shuffledOrder(['a', 'b'], r);
	assert.equal(o.length, 2);
	assert.ok(o.includes('a') && o.includes('b'));
});

test('applyCompareCommand and compareQuery', () => {
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	const trials = blindTrials(panel, 42);
	let s = newCompareSession(trials);
	const ctx = { device: 'ddj-flx4', seed: 42, ref: 'x', pxPerMm: 2 };
	s = applyCompareCommand(s, { type: 'set_mode', mode: 'blind' });
	assert.equal(s.mode, 'blind');
	s = applyCompareCommand(s, { type: 'set_opacity', pct: 30 });
	assert.equal(s.opacity_pct, 30);
	assert.throws(() => applyCompareCommand(s, { type: 'set_opacity', pct: 101 }), /opacity/);
	assert.throws(() => applyCompareCommand(s, { type: 'set_mode', mode: 'nope' }), Error);
	s = applyCompareCommand(s, { type: 'answer', letter: 'A', rating: 4 });
	assert.equal(s.trials[0].rating, 4);
	assert.equal(s.trials[0].choice, s.trials[0].order[0]);
	assert.equal(s.index, 1);
	assert.throws(
		() => applyCompareCommand(newCompareSession(trials), { type: 'goto', index: 99 }),
		/range/
	);
	const q = compareQuery(s, ctx);
	assert.equal(JSON.stringify(q).includes('"order"'), false);
});

test('practice compare IPC', () => {
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	function makeOwner() {
		let session = newCompareSession(blindTrials(panel, 42));
		const ctx = { device: 'ddj-flx4', seed: 42, ref: '', pxPerMm: 2 };
		return {
			dispatch(cmd) {
				session = applyCompareCommand(session, cmd);
				return Object.freeze(compareQuery(session, ctx));
			},
			query() {
				return Object.freeze(compareQuery(session, ctx));
			},
			report() {
				return buildCompareReport({
					device: 'ddj-flx4',
					ref: 'https://example.com/x.jpg',
					seed: 42,
					pxPerMm: 2,
					trials: session.trials,
					recordedAt: '2026-11-06T12:00:00.000Z'
				});
			}
		};
	}
	const ipcA = createPracticeCompareIpc(makeOwner());
	const ipcB = createPracticeCompareIpc(makeOwner());
	assert.equal(ipcA.version, 1);
	assert.ok(Object.isFrozen(ipcA));
	assert.deepEqual(ipcA.dispatch({ type: 'answer', letter: 'A', rating: 4 }), ipcB.answer('A', 4));
	assert.throws(() => ipcA.answer('C', 4), TypeError);
	assert.throws(() => ipcA.dispatch({ type: 'nope' }), TypeError);
	const rep = ipcA.report();
	assert.equal(rep.trials.length, 1);
	assert.ok(rep.trials[0].trial_id);
	assert.ok(Array.isArray(rep.trials[0].order));
	assert.equal(typeof rep.trials[0].rating, 'number');
	assert.ok(rep.trials[0].choice === 'replica' || rep.trials[0].choice === 'reference');
});

test('vectors JSON matches TypeScript implementation', () => {
	const vec = JSON.parse(readFileSync(VECTORS_PATH, 'utf8'));
	for (const [seedStr, expected] of Object.entries(vec.mulberry32)) {
		const seed = Number(seedStr) >>> 0;
		const r = mulberry32(seed);
		const got = Array.from({ length: 8 }, () => r());
		assert.deepEqual(got, expected);
	}
	const layout = JSON.parse(readFileSync(PANEL_PATH, 'utf8'));
	const panel = layout.panel;
	for (const [seedStr, expected] of Object.entries(vec.blind_trials)) {
		const seed = Number(seedStr);
		const got = blindTrials(panel, seed);
		assert.deepEqual(got, expected);
	}
	let tr = blindTrials(panel, 42);
	tr = recordAnswer(tr, 't1', 'A', 4);
	tr = recordAnswer(tr, 't2', 'B', 2);
	const gotReport = buildCompareReport({
		device: 'ddj-flx4',
		ref: 'https://example.com/x.jpg',
		seed: 42,
		pxPerMm: 2,
		trials: tr,
		recordedAt: '2026-11-06T12:00:00.000Z'
	});
	assert.deepEqual(gotReport, vec.report);
});

test('parseCompareCommand rejects non-objects', () => {
	assert.throws(() => parseCompareCommand(null), TypeError);
});
