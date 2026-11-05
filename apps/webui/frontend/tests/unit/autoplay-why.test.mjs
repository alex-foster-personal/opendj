// requirement: AUTOPLAY-8881, AUTOPLAY-8881b
// [if] no decision is stored [then] the readout says so rather than failing, [else stop].
// [if] the decided pair has played and has no verdict [then] the one-tap verdict is offered, [else stop].
// [if] a verdict is given [then] it posts the pair and the verdict to the record endpoint, [else stop].
// [if] VITE_API_BASE names a remote daemon [then] both calls go there, not to the page's origin, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let why;
const API_BASE = 'http://remote-daemon.example.test:8690';

before(async () => {
	why = await loadTypeScriptModule('src/lib/rb/autoplay-why.ts', { viteApiBase: API_BASE });
});

const record = (over = {}) => ({
	out_track_id: 'out',
	in_track_id: 'in',
	decided_at: 'now',
	decision: { reason: 'r', shortlist: ['in'], rejected: [], candidates: [], chosen: null },
	played: { style: 'long_blend', times_played: 1 },
	verdict: null,
	verdict_at: null,
	...over
});

const json = (status, body) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

test('no stored decision reads as null, other failures throw, and the remote daemon is asked', async () => {
	let asked;
	const missing = async (request) => {
		asked = request.url;
		return json(404, { error: 'decision_not_found', message: 'none' });
	};
	assert.equal(await why.fetchLastDecision(missing), null);
	assert.equal(
		asked,
		`${API_BASE}${why.LAST_URL}`,
		'if the page origin is asked instead of VITE_API_BASE then broken'
	);
	await assert.rejects(
		why.fetchLastDecision(async () => json(500, { error: 'boom', message: 'boom' })),
		/why this track/
	);
});

test('the verdict is offered only after the pair played and before a verdict', () => {
	assert.equal(why.verdictOffered(record()), true);
	assert.equal(
		why.verdictOffered(record({ played: null })),
		false,
		'if an unplayed pair is offered a verdict then broken'
	);
	assert.equal(why.verdictOffered(record({ verdict: 'good' })), false);
	assert.equal(why.verdictOffered(record({ in_track_id: '' })), false);
	assert.equal(why.verdictOffered(null), false);
});

test('a verdict posts the pair and the verdict', async () => {
	let sent;
	await why.postVerdict(record(), 'meh', async (request) => {
		sent = { url: request.url, body: await request.json() };
		return json(200, { out_track_id: 'out', in_track_id: 'in', verdict: 'meh' });
	});
	assert.deepEqual(sent, {
		url: `${API_BASE}${why.VERDICT_URL}`,
		body: { out: 'out', in: 'in', verdict: 'meh' }
	});
});
