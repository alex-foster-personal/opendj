/**
 * Framework-neutral comment pins against a stub feedback API (issue #11295).
 */
import assert from 'node:assert/strict';
import { before, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let apiMod;
let uxMod;
let constantsMod;

const PIN = {
	id: 'abc123',
	x_pct: 40,
	y_pct: 30,
	page: '/demo',
	text: 'hello',
	created_at: '2026-11-01T00:00:00.000000Z',
	status: 'open',
	author: 'operator',
	anchor: '#target',
	replies: [],
	attachment: null,
	build: { git_sha: 'deadbeef', built_at_utc: '2026-11-01T00:00:00.000000Z', source: 'test' }
};

before(async () => {
	[apiMod, uxMod, constantsMod] = await Promise.all([
		loadTypeScriptModule('src/lib/components/comment-pins/comment-pins-api.ts'),
		loadTypeScriptModule('src/lib/components/comment-pins/comment-pins-ux.ts'),
		loadTypeScriptModule('src/lib/components/comment-pins/comment-pins-constants.ts')
	]);
});

function stubFetch(handlers) {
	return mock.fn(async (url, init = {}) => {
		const method = init.method ?? 'GET';
		const key = `${method} ${url}`;
		const handler = handlers[key];
		if (handler === undefined) throw new Error(`unexpected fetch ${key}`);
		return handler(init);
	});
}

test('[if] stub comments API [then] drop reply attach archive and resolve succeed without Svelte, [else stop]', async () => {
	const base = 'http://stub.test';
	const paths = apiMod.commentPathsForPrefix('/feedback');
	let store = { comments: [] };
	const replied = {
		...PIN,
		replies: [{ id: 'r1', author: 'operator', text: 'more', created_at: PIN.created_at }]
	};
	const resolved = { ...PIN, status: 'fixed', fixed_in_sha: 'cafebabe', fixed_at: PIN.created_at };
	const attached = {
		...PIN,
		attachment: {
			id: 'att1',
			content_type: 'image/png',
			size_bytes: 12,
			url: '/feedback/attachments/att1'
		}
	};
	const fetchFn = stubFetch({
		[`GET ${base}${paths.comments}`]: async () =>
			new Response(JSON.stringify({ comments: store.comments }), { status: 200 }),
		[`POST ${base}${paths.comments}`]: async (init) => {
			const body = JSON.parse(String(init.body));
			const created = { ...PIN, ...body, id: 'new1' };
			store.comments.push(created);
			return new Response(JSON.stringify(created), { status: 200 });
		},
		[`POST ${base}${paths.replies('abc123')}`]: async () =>
			new Response(JSON.stringify(replied), { status: 200 }),
		[`POST ${base}${paths.archive('abc123')}`]: async () =>
			new Response(JSON.stringify({ archived: true }), { status: 200 }),
		[`PATCH ${base}${paths.patch('abc123')}`]: async () =>
			new Response(JSON.stringify(resolved), { status: 200 }),
		[`POST ${base}${paths.attachment('abc123')}`]: async () =>
			new Response(JSON.stringify(attached), { status: 200 })
	});
	const client = new apiMod.CommentPinsApi({ apiBase: base, paths, fetchFn });
	store.comments = [PIN];
	const listed = await client.listComments();
	assert.equal(listed.length, 1);
	const dropped = await client.createComment({
		text: 'drop',
		x_pct: 1,
		y_pct: 2,
		page: '/demo',
		author: 'operator'
	});
	assert.equal(dropped.id, 'new1');
	const reply = await client.addReply('abc123', 'more');
	assert.equal(reply.replies.length, 1);
	const fixed = await client.patchComment('abc123', { status: 'fixed' });
	assert.equal(fixed.status, 'fixed');
	const withAtt = await client.uploadAttachment(
		'abc123',
		new File(['x'], 'a.png', { type: 'image/png' })
	);
	assert.ok(withAtt.attachment);
	await client.archiveComment('abc123');
});

test('[if] anchor layout drifts [then] exposure reports anchor-moved, [else stop]', () => {
	const exposure = uxMod.pinExposure(PIN, '/demo', true);
	assert.equal(exposure.anchorMoved, true);
	assert.ok(exposure.badges.includes('anchor-moved'));
});

test('[if] pin is harvested [then] exposure reports harvested state, [else stop]', () => {
	const harvested = { ...PIN, status: 'harvested', harvested_at: '2026-11-02T00:00:00.000000Z' };
	const exposure = uxMod.pinExposure(harvested, '/demo', false);
	assert.equal(exposure.harvested, true);
	assert.equal(exposure.collapsed, true);
});

test('[if] comment-pins defines element tag [then] the tag name is comment-pins, [else stop]', () => {
	assert.equal(constantsMod.COMMENT_PINS_TAG, 'comment-pins');
});

test('[if] no fetchFn is injected [then] the default fetch runs with globalThis as receiver, [else stop]', async () => {
	/** Default client must not call native fetch with the CommentPinsApi instance as `this` (#12617). */
	const original = globalThis.fetch;
	const receivers = [];
	globalThis.fetch = function (url) {
		receivers.push(this);
		if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
		return Promise.resolve(new Response(JSON.stringify({ comments: [PIN] }), { status: 200 }));
	};
	try {
		const client = new apiMod.CommentPinsApi({ apiBase: 'http://x' });
		const comments = await client.listComments();
		assert.equal(comments.length, 1);
		assert.equal(receivers.length, 1);
	} finally {
		globalThis.fetch = original;
	}
});
