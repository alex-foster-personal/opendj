/**
 * ANIM-01: the dial Animate box's parser, held to the SAME table as the
 * Python twin (tests/opendj_cli/fixtures/animate_expr_vectors.json), plus the
 * ramp executor's take-over and replace rules (ANIM-02), on the real
 * dispatcher with no mocking.
 *
 * [if] an accept row does not give its to and Duration [then ⛔]
 * [if] a refuse row passes, or its one line does not name the failing token [then ⛔]
 * [if] a hand move during a ramp does not stop it, or the dot stays on [then ⛔]
 * [if] a second animation on a control does not replace the first [then ⛔]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };

const expr = await loadTypeScriptModule('src/lib/rb/animate-expr.ts');
const entry = await loadTypeScriptModule('tests/unit/fixtures/agent-orders-entry.ts');
const vectors = JSON.parse(
	readFileSync(new URL('../../../../../tests/opendj_cli/fixtures/animate_expr_vectors.json', import.meta.url), 'utf8')
);

afterEach(() => {
	delete globalThis.window;
});

for (const row of vectors.accept) {
	test(`accepts ${JSON.stringify(row.expr)}`, () => {
		assert.deepEqual(expr.parseAnimateExpr(row.expr), { ok: true, to: row.to, over: row.over });
	});
}

for (const row of vectors.refuse) {
	test(`refuses ${JSON.stringify(row.expr)} naming ${row.token}`, () => {
		const parse = expr.parseAnimateExpr(row.expr);
		assert.deepEqual(parse, { ok: false, token: row.token, message: row.message });
		assert.equal(expr.animateErrorLine(parse), row.token === null ? row.message : `"${row.token}": ${row.message}`);
	});
}

test("the maintainer's EQ LOW example becomes the CLI's own ramp payload", () => {
	assert.deepEqual(expr.animateRampPayload(1, 'eq_low', '0 --over 8bars --anchor next_downbeat'), {
		command: { type: 'eq', deck: 1, band: 'low', value: 0 },
		to: 0,
		over: { unit: 'bars', n: 8, anchor: 'next_downbeat' }
	});
	assert.equal(
		expr.animateCli(1, 'eq_low', ' 0  --over 8bars --anchor next_downbeat'),
		'opendj animate 1 eq_low "0 --over 8bars --anchor next_downbeat"'
	);
	assert.match(expr.animateHint('eq_low'), /e\.g\. 0 --over 8bars --anchor next_downbeat$/);
});

test('a bad expression or deck throws its one line before any ramp exists', () => {
	assert.throws(() => expr.animateRampPayload(1, 'eq_low', '@8 0'), /^SyntaxError: "@8": expected a target value/);
	assert.throws(() => expr.animateRampPayload(null, 'eq_low', '0 --over 8bars'), /eq_low needs deck 1\.\.4/);
	assert.throws(() => expr.animateRampPayload(1, 'crossfader', '0 --over 8bars'), /crossfader belongs to no deck/);
});

test('every right-clickable dial id resolves to its control and back to the same indicator key', () => {
	for (const [id, control] of [
		['1:low', 'eq_low'], ['2:mid', 'eq_mid'], ['3:high', 'eq_high'], ['4:trim', 'trim'], ['1:filter', 'filter'],
		['2:fader', 'fader'], ['3:stem-vocal', 'stem_vocal'], ['4:stem-bass', 'stem_bass'], ['crossfader', 'crossfader']
	]) {
		const target = expr.animateTargetOfKnobId(id);
		assert.equal(target?.control, control, id);
		const payload = expr.animateRampPayload(target.deck, target.control, '0 --over 1beat');
		assert.equal(expr.animationKeyOf(payload.command), id);
	}
	assert.equal(expr.animateTargetOfKnobId('hp:hp-mix'), null);
});

test('a hand move mid-ramp takes over: the ramp stops and its running dot clears (ANIM-02)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		let started = false;
		const running = entry.runRamp(expr.animateRampPayload(1, 'eq_low', '0 --over 2000ms --clock 1'), {
			source: 'user-ui',
			label: 'EQ LOW',
			onStarted: () => (started = true)
		});
		assert.equal(started, true);
		assert.deepEqual(entry.dialAnimations.running['1:low'], { label: 'EQ LOW' });
		await new Promise((resolve) => setTimeout(resolve, 60));
		await entry.dispatchPerformanceCommand({ type: 'eq', deck: 1, band: 'low', value: 0.9 }, undefined, 'user-ui');
		const result = await running;
		assert.equal(result.status, 'failed');
		assert.match(result.error, /take-over/);
		assert.equal(entry.dialAnimations.running['1:low'], undefined);
		assert.equal(entry.queryPerformanceState().mixer.channels[1].eq_low, 0.9);
	} finally {
		uninstall();
	}
});

test('a second animation on the same control replaces the first (ANIM-02)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const first = entry.runRamp(expr.animateRampPayload(2, 'fader', '0 --over 2000ms --clock 1'), { source: 'user-ui' });
		await new Promise((resolve) => setTimeout(resolve, 40));
		const second = entry.runRamp(expr.animateRampPayload(2, 'fader', '1 --over 40ms --clock 1'), { source: 'user-ui' });
		const [a, b] = await Promise.all([first, second]);
		assert.equal(a.status, 'failed');
		assert.match(a.error, /replaced by a newer animation/);
		assert.equal(b.status, 'succeeded');
		assert.equal(entry.queryPerformanceState().mixer.channels[2].fader, 1);
		assert.equal(entry.dialAnimations.running['2:fader'], undefined);
	} finally {
		uninstall();
	}
});

test('a refusal at start is explained in the box, other messages pass through', () => {
	assert.match(expr.animateStartRefusal('no_master'), /^no_master: no deck is master/);
	assert.match(expr.animateStartRefusal('clock_not_playing'), /use an ms duration$/);
	assert.equal(expr.animateStartRefusal('something else'), 'something else');
});
