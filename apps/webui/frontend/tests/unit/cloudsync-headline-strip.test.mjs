// requirement CSSTATUS-9152 (issue #8720): shared headline strip above every /cloudsync tab
//
// [if] any of the five /cloudsync tabs is showing [then] the headline strip is visible above the tab bar [else stop]
// [if] the strip view is SSR-rendered for mockup states [then] the correct action button shows [else stop]
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FAKE_NAV = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));
const FAKE_STORES = fileURLToPath(new URL('./fake-app-stores.mjs', import.meta.url));

const PAGE_ENTRY = [
	"export { default as CloudSyncPage } from './src/routes/cloudsync/+page.svelte';",
	"export { CLOUDSYNC_TABS } from '$lib/components/cloudsync/cloudsync-view';",
	"export { render } from 'svelte/server';"
].join('\n');

const VIEW_ENTRY = [
	"export { default as CloudSyncHeadlineStripView } from '$lib/components/cloudsync/CloudSyncHeadlineStripView.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const post = {
	kind: 'post',
	body: { hub_url: 'http://box.example-tailnet.ts.net:8686', name: 'gig-laptop', force: false }
};

let pageMod;
let viewMod;
let view;

before(async () => {
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
	pageMod = await loadSvelteSsrModule(PAGE_ENTRY, {
		alias: { '$app/navigation': FAKE_NAV, '$app/stores': FAKE_STORES }
	});
	viewMod = await loadSvelteSsrModule(VIEW_ENTRY);
});

function stripIndex(html) {
	const at = html.indexOf('data-testid="cloudsync-headline-strip"');
	assert.ok(at >= 0, 'headline strip must render');
	return at;
}

function baseProps() {
	return {
		syncNowLabel: view.SYNC_NOW_LABEL,
		refreshLabel: view.HEADLINE_REFRESH_LABEL,
		setUpLabel: view.SET_UP_LABEL,
		syncing: false,
		refreshing: false,
		gateLoadError: null,
		syncDecision: post,
		notice: null,
		onSyncNow: () => {},
		onRefresh: () => {},
		onSetUp: () => {}
	};
}

test('headline strip renders above tab bar on every CloudSync tab', () => {
	for (const { id } of pageMod.CLOUDSYNC_TABS) {
		const query = id === 'status' ? '' : `?tab=${id}`;
		globalThis.__pageUrl = new URL(`http://127.0.0.1/cloudsync${query}`);
		const html = pageMod.render(pageMod.CloudSyncPage).body;
		const stripAt = stripIndex(html);
		const tablistAt = html.indexOf('role="tablist"');
		assert.ok(tablistAt >= 0, `tab bar must render for tab ${id}`);
		assert.ok(stripAt < tablistAt, `strip must appear before tab bar for tab ${id}`);
		assert.match(html, /data-testid="cloudsync-headline-refresh"/);
	}
});

test('in-sync strip view shows Sync now', () => {
	const html = viewMod.render(viewMod.CloudSyncHeadlineStripView, {
		props: {
			...baseProps(),
			headline: { tone: 'ok', text: 'In sync. Last synced 4m ago.' },
			action: 'sync-now'
		}
	}).body;
	assert.match(html, /data-testid="cloudsync-headline-sync-now"/);
	assert.match(html, new RegExp(view.SYNC_NOW_LABEL));
});

test('syncing strip view shows Refresh', () => {
	const html = viewMod.render(viewMod.CloudSyncHeadlineStripView, {
		props: {
			...baseProps(),
			headline: { tone: 'warn', text: 'Syncing...' },
			action: 'refresh'
		}
	}).body;
	assert.match(html, /data-testid="cloudsync-headline-refresh"/);
	assert.match(html, new RegExp(view.HEADLINE_REFRESH_LABEL));
});

test('not-set-up strip view shows Set up', () => {
	const html = viewMod.render(viewMod.CloudSyncHeadlineStripView, {
		props: {
			...baseProps(),
			headline: {
				tone: 'off',
				text: 'CloudSync is not set up on this machine. Enter a hub URL below and save to turn it on.'
			},
			action: 'set-up'
		}
	}).body;
	assert.match(html, /data-testid="cloudsync-headline-set-up"/);
	assert.match(html, /Set up →/);
});

test('transport-error strip view shows Sync now', () => {
	const html = viewMod.render(viewMod.CloudSyncHeadlineStripView, {
		props: {
			...baseProps(),
			headline: {
				tone: 'error',
				text: 'Not synced: could not reach the hub machine (connection refused). Check the hub URL below and that the hub machine is running, then try Sync now again.'
			},
			action: 'sync-now'
		}
	}).body;
	assert.match(html, /data-testid="cloudsync-headline-sync-now"/);
});

test('update_required strip headline mentions App update required (CSSTATUS-06)', () => {
	const updateRequired = {
		code: 'SYNC_WIRE_VERSION',
		local_wire_version: 3,
		peer_wire_version: 4,
		action: 'install the latest Open DJ'
	};
	const headline = view.statusHeadline({
		enabled: true,
		configured: true,
		running: true,
		heartbeat_at: null,
		enabled_source: 'file',
		endpoint_source: 'file',
		reason: null,
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: null,
		rows_pending: null,
		hash_pending: null,
		quarantined: null,
		excluded_total: null,
		endpoint: 'http://box.example-tailnet.ts.net:8686',
		recent_results: [],
		update_required: updateRequired
	});
	const html = viewMod.render(viewMod.CloudSyncHeadlineStripView, {
		props: {
			...baseProps(),
			headline,
			action: 'refresh'
		}
	}).body;
	assert.match(html, /App update required/);
});
