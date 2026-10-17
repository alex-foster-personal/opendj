import assert from 'node:assert/strict';
import test from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const API_BASE = 'https://store-build.example.test';
const ACCOUNT_PATH = '/api/v1/account';
const ELIGIBILITY_PATH = '/api/v1/sync/billing/eligibility';
const REFRESH_PATH = '/api/v1/sync/billing/refresh';

function requestUrl(input) {
	return typeof input === 'string' ? input : input.url;
}

function routedFetch(handlers) {
	const declared = Object.keys(handlers);
	return async (input) => {
		const { pathname } = new URL(requestUrl(input), API_BASE);
		const handler = handlers[pathname];
		if (handler === undefined) {
			throw new Error(`unexpected fetch to ${pathname}; declared: ${declared.join(', ')}`);
		}
		return handler();
	};
}

let overlayBundlePromise;
function overlayBundle() {
	overlayBundlePromise ??= bundleSvelteEntry(`
		export { default as AccountOverlayComponent } from '$lib/components/account/AccountOverlay.svelte';
		export { openAccountOverlay } from '$lib/account/overlay.svelte';
		export { accountStore } from '$lib/account/account-store.svelte';
		export { hostedBilling } from '$lib/account/hosted-billing.svelte';
		export {
			loadHostedBilling,
			onHostedBillingWindowFocus,
			openHostedCheckout
		} from '$lib/account/hosted-billing-api';
		export { buildFlags } from '$lib/api/store-build.svelte';
		export { entitlements } from '$lib/api/entitlements.svelte';
	`);
	return overlayBundlePromise;
}

function accountResponse() {
	return new Response(
		JSON.stringify({
			signed_in: true,
			user: {
				google_sub: 'sub-1',
				email: 'dj@example.test',
				name: 'DJ',
				avatar_url: null,
				created_at: '2026-01-01T00:00:00Z'
			},
			plan: { plan_id: 'free', label: 'Free', note: 'no gate', provider: null },
			authorisation_enforced: false,
			authorisation_note: 'sign-in is identity, not authorisation',
			local_data: [],
			privacy_policy_url: 'https://open-dj.com/privacy'
		}),
		{ status: 200, headers: { 'content-type': 'application/json' } }
	);
}

const FLAGS_BODY = {
	profile: 'dev',
	sandboxed: false,
	flags: []
};

const ENTITLEMENTS_BODY = {
	plan: { plan_id: 'free', label: 'Free' },
	features: []
};

test('account overlay shows Subscribe and Manage only for a Polar hosted hub', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ACCOUNT_PATH]: accountResponse,
		'/api/v1/flags': () =>
			new Response(JSON.stringify(FLAGS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		'/api/v1/entitlements': () =>
			new Response(JSON.stringify(ENTITLEMENTS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		[ELIGIBILITY_PATH]: () =>
			new Response(
				JSON.stringify({
					eligible: true,
					hosted: true,
					entitlement_provider: 'polar:sandbox'
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			),
		[REFRESH_PATH]: () =>
			new Response(
				JSON.stringify({
					provider: 'polar:sandbox',
					standings: [{ feature_id: 'cloudsync.hosted_hub', state: 'active' }]
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			)
	});

	await bundle.entitlements.load();
	await bundle.buildFlags.load();
	await bundle.accountStore.load();
	await bundle.loadHostedBilling();
	bundle.openAccountOverlay();
	const html = await renderToHtml(bundle.AccountOverlayComponent);

	assert.match(html, /Subscribe/);
	assert.match(html, /Manage/);
	assert.match(html, /cloudsync\.hosted_hub/);
});

test('account overlay hides hosted billing controls when the hub is not Polar', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ACCOUNT_PATH]: accountResponse,
		'/api/v1/flags': () =>
			new Response(JSON.stringify(FLAGS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		'/api/v1/entitlements': () =>
			new Response(JSON.stringify(ENTITLEMENTS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		[ELIGIBILITY_PATH]: () =>
			new Response(
				JSON.stringify({
					eligible: false,
					hosted: false,
					entitlement_provider: null
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			)
	});

	await bundle.entitlements.load();
	await bundle.buildFlags.load();
	await bundle.accountStore.load();
	await bundle.loadHostedBilling();
	bundle.openAccountOverlay();
	const html = await renderToHtml(bundle.AccountOverlayComponent);

	assert.equal(html.includes('Subscribe'), false);
	assert.equal(html.includes('CloudSync hosted hub'), false);
});

test('account overlay shows a refresh failure on an eligible hub instead of hiding billing', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ACCOUNT_PATH]: accountResponse,
		'/api/v1/flags': () =>
			new Response(JSON.stringify(FLAGS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		'/api/v1/entitlements': () =>
			new Response(JSON.stringify(ENTITLEMENTS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		[ELIGIBILITY_PATH]: () =>
			new Response(
				JSON.stringify({
					eligible: true,
					hosted: true,
					entitlement_provider: 'polar:sandbox'
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			),
		[REFRESH_PATH]: () =>
			new Response(
				JSON.stringify({
					detail: { code: 'SYNC_ENTITLEMENT_PROVIDER', message: 'polar is down' }
				}),
				{ status: 503, headers: { 'content-type': 'application/json' } }
			)
	});

	await bundle.entitlements.load();
	await bundle.buildFlags.load();
	await bundle.accountStore.load();
	await bundle.loadHostedBilling();
	assert.equal(bundle.hostedBilling.eligible, true);
	assert.notEqual(bundle.hostedBilling.error, null);
	bundle.openAccountOverlay();
	const html = await renderToHtml(bundle.AccountOverlayComponent);

	assert.match(html, /Could not read hosted billing/);
});

test('Subscribe in a desktop shell opens the checkout through the native opener', async () => {
	const bundle = await overlayBundle();
	const opened = [];
	globalThis.opendjShell = { kind: 'electron', openExternal: async (url) => opened.push(url) };
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () =>
			new Response(
				JSON.stringify({ eligible: true, hosted: true, entitlement_provider: 'polar:sandbox' }),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			),
		[REFRESH_PATH]: () =>
			new Response(JSON.stringify({ provider: 'polar:sandbox', standings: [] }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		'/api/v1/sync/billing/checkout': () =>
			new Response(JSON.stringify({ url: 'https://buy.polar.sh/c/1' }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			})
	});
	try {
		await bundle.loadHostedBilling();
		await bundle.openHostedCheckout();
	} finally {
		delete globalThis.opendjShell;
	}
	assert.deepEqual(opened, ['https://buy.polar.sh/c/1']);
});

test('account overlay shows an eligibility failure instead of hiding billing', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ACCOUNT_PATH]: accountResponse,
		'/api/v1/flags': () =>
			new Response(JSON.stringify(FLAGS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		'/api/v1/entitlements': () =>
			new Response(JSON.stringify(ENTITLEMENTS_BODY), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			}),
		[ELIGIBILITY_PATH]: () =>
			new Response(JSON.stringify({ detail: { code: 'SYNC_HUB_UNREACHABLE', message: 'hub down' } }), {
				status: 503,
				headers: { 'content-type': 'application/json' }
			})
	});

	await bundle.entitlements.load();
	await bundle.buildFlags.load();
	await bundle.accountStore.load();
	await bundle.loadHostedBilling();
	assert.equal(bundle.hostedBilling.eligible, false);
	bundle.openAccountOverlay();
	const html = await renderToHtml(bundle.AccountOverlayComponent);

	assert.match(html, /Could not read hosted billing/);
	assert.equal(html.includes('Subscribe'), false);
});

function json(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

const ELIGIBLE = {
	eligible: true,
	hosted: true,
	entitlement_provider: 'polar:sandbox'
};

test('focus refreshes only after a billing page opened, once, and clears a stale error', async () => {
	const bundle = await overlayBundle();
	const answers = [
		() =>
			json(
				{
					detail: {
						code: 'SYNC_ENTITLEMENT_PROVIDER',
						message: 'polar is down'
					}
				},
				503
			),
		() =>
			json({
				provider: 'polar:sandbox',
				standings: [{ feature_id: 'cloudsync.hosted_hub', state: 'active' }]
			})
	];
	let refreshes = 0;
	globalThis.opendjShell = { kind: 'electron', openExternal: async () => {} };
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
		[REFRESH_PATH]: () => answers[Math.min(refreshes++, answers.length - 1)](),
		'/api/v1/sync/billing/checkout': () => json({ url: 'https://buy.polar.sh/c/1' })
	});
	try {
		await bundle.loadHostedBilling();
		assert.notEqual(bundle.hostedBilling.error, null);
		await bundle.onHostedBillingWindowFocus();
		assert.equal(refreshes, 1, 'a focus with no billing page opened must not refresh');
		await bundle.openHostedCheckout();
		await bundle.onHostedBillingWindowFocus();
		assert.equal(refreshes, 2);
		assert.equal(bundle.hostedBilling.error, null);
		assert.deepEqual(bundle.hostedBilling.standings, [
			{ feature_id: 'cloudsync.hosted_hub', state: 'active' }
		]);
		await bundle.onHostedBillingWindowFocus();
		assert.equal(refreshes, 2, 'one focus refresh disarms it');
	} finally {
		delete globalThis.opendjShell;
	}
});

for (const [name, body] of [
	['eligibility missing eligible', { hosted: true, entitlement_provider: 'polar:sandbox' }],
	['eligibility with a non-string provider', { eligible: true, entitlement_provider: 7 }]
]) {
	test(`a malformed answer is an error, not "not eligible": ${name}`, async () => {
		const bundle = await overlayBundle();
		globalThis.fetch = routedFetch({ [ELIGIBILITY_PATH]: () => json(body) });
		await bundle.loadHostedBilling();
		assert.match(String(bundle.hostedBilling.error), /bad eligibility answer/);
	});
}

test('malformed refresh standings are an error, not "no subscription"', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
		[REFRESH_PATH]: () => json({ provider: 'polar:sandbox', standings: [{ state: 'active' }] })
	});
	await bundle.loadHostedBilling();
	assert.match(String(bundle.hostedBilling.error), /bad refresh answer/);
});

for (const [label, provider] of [
	['missing', undefined],
	['null', null],
	['a number', 7],
	['empty', ''],
	['a non-Polar', 'stripe'],
	['a polar lookalike', 'polaris'],
	['an uppercase polar', 'POLAR']
]) {
	test(`a refresh answer with a ${label} provider is an error and keeps the old state`, async () => {
		const bundle = await overlayBundle();
		globalThis.fetch = routedFetch({
			[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
			[REFRESH_PATH]: () => json({ provider, standings: [{ feature_id: 'x', state: 'active' }] })
		});
		await bundle.loadHostedBilling();
		assert.match(String(bundle.hostedBilling.error), /bad refresh answer/);
		assert.deepEqual(bundle.hostedBilling.standings, []);
	});
}

for (const provider of ['polar', 'polar:sandbox']) {
	test(`a refresh answer from Polar provider ${provider} is accepted`, async () => {
		const bundle = await overlayBundle();
		globalThis.fetch = routedFetch({
			[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
			[REFRESH_PATH]: () => json({ provider, standings: [{ feature_id: 'x', state: 'active' }] })
		});
		await bundle.loadHostedBilling();
		assert.equal(bundle.hostedBilling.error, null);
		assert.equal(bundle.hostedBilling.provider, provider);
	});
}

test('a non-JSON error body is an error that names the status', async () => {
	const bundle = await overlayBundle();
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => new Response('<html>bad gateway</html>', { status: 502 })
	});
	await bundle.loadHostedBilling();
	assert.match(String(bundle.hostedBilling.error), /HTTP 502, no JSON object/);
});

for (const [label, url] of [
	['a javascript: url', 'javascript:alert(1)'],
	['an http polar url', 'http://buy.polar.sh/c/1'],
	['a foreign host', 'https://evil.example.test/c/1'],
	['a lookalike host', 'https://polar.sh.evil.example.test/c/1'],
	['not a url', 'not a url']
]) {
	test(`a checkout answer with ${label} is refused and never opened`, async () => {
		const bundle = await overlayBundle();
		const opened = [];
		globalThis.opendjShell = { kind: 'electron', openExternal: async (u) => opened.push(u) };
		globalThis.fetch = routedFetch({
			[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
			[REFRESH_PATH]: () => json({ provider: 'polar:sandbox', standings: [] }),
			'/api/v1/sync/billing/checkout': () => json({ url })
		});
		try {
			await bundle.loadHostedBilling();
			await assert.rejects(bundle.openHostedCheckout(), /checkout: unsafe url/);
		} finally {
			delete globalThis.opendjShell;
		}
		assert.deepEqual(opened, []);
	});
}

test('a blocked popup in a browser tab is an explicit error, never a same-tab redirect', async () => {
	const bundle = await overlayBundle();
	const assigned = [];
	globalThis.window = { open: () => null, location: { assign: (u) => assigned.push(u) } };
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
		[REFRESH_PATH]: () => json({ provider: 'polar:sandbox', standings: [] }),
		'/api/v1/sync/billing/checkout': () => json({ url: 'https://buy.polar.sh/c/1' })
	});
	try {
		await bundle.loadHostedBilling();
		await assert.rejects(bundle.openHostedCheckout(), /popup blocked/);
	} finally {
		delete globalThis.window;
	}
	assert.deepEqual(assigned, []);
});

test('a browser tab opens the checkout in the tab it opened inside the click', async () => {
	const bundle = await overlayBundle();
	const tab = { opener: 'set', location: { href: 'about:blank' }, close: () => {} };
	globalThis.window = { open: () => tab, location: { assign: () => assert.fail('same-tab') } };
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
		[REFRESH_PATH]: () => json({ provider: 'polar:sandbox', standings: [] }),
		'/api/v1/sync/billing/checkout': () => json({ url: 'https://buy.polar.sh/c/1' })
	});
	try {
		await bundle.loadHostedBilling();
		await bundle.openHostedCheckout();
	} finally {
		delete globalThis.window;
	}
	assert.equal(tab.location.href, 'https://buy.polar.sh/c/1');
	assert.equal(tab.opener, null);
});

for (const state of ['paid', 'ACTIVE', '', 3]) {
	test(`a refresh standing with state ${JSON.stringify(state)} is an error and keeps the old state`, async () => {
		const bundle = await overlayBundle();
		globalThis.fetch = routedFetch({
			[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
			[REFRESH_PATH]: () =>
				json({ provider: 'polar:sandbox', standings: [{ feature_id: 'cloudsync.hosted_hub', state }] })
		});
		await bundle.loadHostedBilling();
		assert.match(String(bundle.hostedBilling.error), /bad refresh answer/);
		assert.deepEqual(bundle.hostedBilling.standings, []);
	});
}

test('every hub lifecycle state and null are accepted standings', async () => {
	const bundle = await overlayBundle();
	const standings = ['active', 'past_due', 'read_only', 'archived', null].map((state, i) => ({
		feature_id: `f${i}`,
		state
	}));
	globalThis.fetch = routedFetch({
		[ELIGIBILITY_PATH]: () => json(ELIGIBLE),
		[REFRESH_PATH]: () => json({ provider: 'polar:sandbox', standings })
	});
	await bundle.loadHostedBilling();
	assert.equal(bundle.hostedBilling.error, null);
	assert.deepEqual(bundle.hostedBilling.standings, standings);
});
