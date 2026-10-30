// requirement: AGENT-48
// [if] the planner answers an order [then] the planner path runs with the pick, [else stop].
// [if] the planner answers no order, a refusal or nothing [then] the featurette runs with that reason, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let plan;

before(async () => {
	plan = await loadTypeScriptModule('src/lib/rb/auto-play-next-plan.ts');
});

const pick = { candidate: 'c1', style: 'long_blend', bars: 32, score: 0.41, out_start_bar: 97, rationale: 'why' };
const answer = (over = {}) => ({
	order: { plan: { id: 'plan-1' } },
	reason: 'intent smooth',
	intent: 'smooth',
	pick,
	out_bar_now: 40.2,
	considered: 42,
	rendered: 9,
	propose_ms: 10,
	render_ms: 20,
	scored: true,
	plan_ms: 5,
	saved: false,
	plan: { style: 'long_blend' },
	...over
});
const request = { out_deck: 1, in_deck: 2, intent: 'smooth', recent_styles: ['wave'] };
const signal = new AbortController().signal;
const fetched = (status, body) => async () => new Response(JSON.stringify(body), { status });

test('an order selects the planner path with its pick', () => {
	const choice = plan.selectTransitionPath(answer());
	assert.equal(choice.path, 'planner');
	assert.equal(choice.plan.id, 'plan-1');
	assert.equal(choice.pick.style, 'long_blend');
});

test('no order selects the featurette with the planner reason', () => {
	const choice = plan.selectTransitionPath(answer({ order: null, pick: null, reason: 'nothing eligible' }));
	assert.deepEqual(choice, { path: 'featurette', reason: 'nothing eligible' });
});

test('the request carries the decks, intent and variety memory', async () => {
	let sent;
	const fetchImpl = async (url, init) => {
		sent = { url, body: JSON.parse(init.body) };
		return new Response(JSON.stringify(answer()), { status: 200 });
	};
	await plan.requestPlannedTransition(request, signal, fetchImpl);
	assert.equal(sent.url, plan.PLANNER_URL);
	assert.deepEqual(sent.body, request);
});

test('a refusal and an unreachable planner are reasons, never exceptions', async () => {
	const refused = await plan.requestPlannedTransition(request, signal, fetched(422, { error: 'deck_empty', message: 'deck 2 holds no track' }));
	assert.deepEqual(refused, { path: 'featurette', reason: 'planner answered 422: deck 2 holds no track' });
	const unreadable = await plan.requestPlannedTransition(request, signal, async () => new Response('boom', { status: 500 }));
	assert.deepEqual(unreadable, { path: 'featurette', reason: 'planner answered 500: boom' });
	const down = await plan.requestPlannedTransition(request, signal, async () => {
		throw new TypeError('Load failed');
	});
	assert.deepEqual(down, { path: 'featurette', reason: 'planner unreachable: Load failed' });
});

test('the readout names the path and its hover says why', () => {
	const planned = plan.planRunOf(plan.selectTransitionPath(answer()), 1234.4);
	assert.equal(plan.runLabel(planned), 'plan: long_blend 32');
	assert.match(plan.runTitle(planned), /bar 97.*9 scored candidates \(1234 ms planning\)/s);
	const fixed = plan.planRunOf({ path: 'featurette', reason: 'no plan' }, 5);
	assert.equal(plan.runLabel(fixed), 'fixed loop');
	assert.match(plan.runTitle(fixed), /not planned: no plan/);
});

test('a 200 the page cannot act on falls back with the reason', async () => {
	const noPlanId = await plan.requestPlannedTransition(request, signal, fetched(200, answer({ order: { plan: {} } })));
	assert.deepEqual(noPlanId, { path: 'featurette', reason: 'planner answer malformed: order carries no plan id' });
	const noPick = await plan.requestPlannedTransition(request, signal, fetched(200, answer({ pick: null })));
	assert.deepEqual(noPick, { path: 'featurette', reason: 'planner answer malformed: an order without its pick' });
	const notJson = await plan.requestPlannedTransition(request, signal, async () => new Response('<html>', { status: 200 }));
	assert.equal(notJson.path, 'featurette');
	// Control: a well-formed answer still selects the planner.
	assert.equal((await plan.requestPlannedTransition(request, signal, fetched(200, answer()))).path, 'planner');
});

test('an unscored pick runs the planner and the readout says M is not measured', async () => {
	const unscored = answer({ scored: false, pick: { ...pick, score: null }, rendered: 0 });
	const choice = await plan.requestPlannedTransition(request, signal, fetched(200, unscored));
	assert.equal(choice.path, 'planner');
	const run = plan.planRunOf(choice, 12);
	assert.equal(plan.runLabel(run), 'plan: long_blend 32 (unscored)');
	assert.match(plan.runTitle(run), /not yet scored/);
});

test('prepare posts the pair and a refusal is an error the watch can retry', async () => {
	let sent;
	await plan.requestPrepare({ out_deck: 1, in_deck: 2, intent: 'smooth' }, async (url, init) => {
		sent = { url, body: JSON.parse(init.body) };
		return new Response('{}', { status: 200 });
	});
	assert.equal(sent.url, plan.PREPARE_URL);
	assert.deepEqual(sent.body, { out_deck: 1, in_deck: 2, intent: 'smooth' });
	await assert.rejects(
		plan.requestPrepare({ out_deck: 1, in_deck: 2, intent: 'smooth' }, fetched(409, { message: 'no page' })),
		/planner answered 409: no page/
	);
});

test('a completed plan is saved for the pair; a replay only counts', async () => {
	const sent = [];
	const fetchImpl = async (_url, init) => {
		sent.push(JSON.parse(init.body));
		return new Response('{}', { status: 200 });
	};
	await plan.reportPlayed({ out: 'a', in: 'b', saved: false, plan: { id: 1 }, style: 'wave' }, fetchImpl);
	await plan.reportPlayed({ out: 'a', in: 'b', saved: true, plan: { id: 1 }, style: 'wave' }, fetchImpl);
	assert.deepEqual(sent, [
		{ out: 'a', in: 'b', plan: { id: 1 }, source: 'planner', style: 'wave' },
		{ out: 'a', in: 'b' }
	]);
	await assert.rejects(
		plan.reportPlayed({ out: 'a', in: 'b', saved: true, plan: null, style: 'wave' }, async () => new Response('gone', { status: 404 })),
		/played answered 404/
	);
});

test('a saved replay reads as saved on the readout', () => {
	const run = plan.planRunOf(plan.selectTransitionPath(answer({ saved: true, scored: false, pick: { ...pick, score: null } })), 3);
	assert.equal(plan.runLabel(run), 'saved: long_blend 32');
	assert.match(plan.runTitle(run), /Saved transition for this pair/);
});
