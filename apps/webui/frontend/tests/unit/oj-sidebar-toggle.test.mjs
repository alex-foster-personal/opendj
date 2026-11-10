/** OJ-9477 / AISIDE-11839: the top bar OJ button, the oj_toggle command and the sidebar's motion gate. */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let rules;
let state;
let guards;
let ipc;
let ssr;

const SSR_ENTRY = [
	"export { default as OjTopBarButton } from '$lib/components/rb/OjTopBarButton.svelte';",
	"export { default as AgentSidebarMount } from '$lib/components/assistant/AgentSidebarMount.svelte';",
	"export { setOjSidebarOpen, toggleOjSidebar } from '$lib/assistant/oj-sidebar.svelte';",
	"export { fakeAssistantProbe } from '$lib/assistant/status-probe.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const FAKE_PROBE = fileURLToPath(new URL('./fake-assistant-status-probe.mjs', import.meta.url));

before(async () => {
	rules = await loadTypeScriptModule('src/lib/assistant/oj-sidebar-toggle.ts');
	state = await loadTypeScriptModule('src/lib/assistant/oj-sidebar.svelte.ts');
	guards = await loadTypeScriptModule('src/lib/rb/performance-ipc-guards.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	ssr = await loadSvelteSsrModule(SSR_ENTRY, {
		alias: { '$lib/assistant/status-probe.svelte': FAKE_PROBE }
	});
});

test('if the assistant is unconfigured, unread, failing or the flag is off then no OJ button renders', () => {
	assert.equal(rules.ojTopBarButtonVisible(true, { configured: true }, null), true);
	assert.equal(rules.ojTopBarButtonVisible(true, { configured: false }, null), false);
	assert.equal(rules.ojTopBarButtonVisible(true, null, null), false, 'still checking');
	assert.equal(rules.ojTopBarButtonVisible(true, null, 'boom'), false, 'status read failed');
	assert.equal(rules.ojTopBarButtonVisible(false, { configured: true }, null), false, 'flag off');
});

test('if a deck plays on /performance or reduced motion is on then the panel opens with no animation', () => {
	const base = { anyDeckPlaying: false, performanceVisible: true, reducedMotion: false };
	assert.equal(rules.ojSidebarAnimates(base), true);
	assert.equal(rules.ojSidebarAnimates({ ...base, anyDeckPlaying: true }), false);
	assert.equal(rules.ojSidebarAnimates({ ...base, reducedMotion: true }), false);
	assert.equal(
		rules.ojSidebarAnimates({ ...base, anyDeckPlaying: true, performanceVisible: false }),
		true,
		'a deck playing off /performance does not still the panel'
	);
});

test('if toggled twice then the shared sidebar state opens then closes', () => {
	assert.equal(state.ojSidebar.open, false);
	state.toggleOjSidebar();
	assert.equal(state.ojSidebar.open, true);
	state.toggleOjSidebar();
	assert.equal(state.ojSidebar.open, false);
});

test('if an agent sends oj_toggle then it parses, skips the deck queue and flips the sidebar', async () => {
	assert.deepEqual(guards._parseCommand({ type: 'oj_toggle' }), { type: 'oj_toggle' });
	assert.throws(() => guards._parseCommand({ type: 'oj_toggle', extra: 1 }));
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'oj_toggle' }), null);
});

test('if oj_toggle stops reaching the shared sidebar state then broken', async () => {
	const ipcSource = await readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(ipcSource, /command\.type === 'oj_toggle'\) \{\n\t\ttoggleOjSidebar\(\);/);
});

test('if the OJ button renders before the assistant reports configured, or ignores the open state, then broken', () => {
	const html = (props) =>
		ssr.render(ssr.OjTopBarButton, { props: { ontoggle: () => {}, ...props } }).body;
	const on = () => true;
	ssr.setOjSidebarOpen(false);
	ssr.fakeAssistantProbe.status = null;
	ssr.fakeAssistantProbe.statusError = null;
	assert.doesNotMatch(html({ isEnabled: on }), /oj-topbar-button/, 'still checking');
	ssr.fakeAssistantProbe.status = { configured: true };
	assert.doesNotMatch(html({ isEnabled: () => false }), /oj-topbar-button/, 'flag off');
	const closed = html({ isEnabled: on });
	assert.match(closed, /data-testid="oj-topbar-button"/);
	assert.match(closed, /aria-label="Open OJ"/);
	assert.match(closed, /aria-pressed="false"/);
	ssr.setOjSidebarOpen(true);
	const opened = html({ isEnabled: on });
	assert.match(opened, /aria-label="Close OJ"/);
	assert.match(opened, /aria-pressed="true"/);
	ssr.fakeAssistantProbe.statusError = 'boom';
	assert.doesNotMatch(html({ isEnabled: on }), /oj-topbar-button/, 'status read failed');
	ssr.setOjSidebarOpen(false);
});

test('if the sidebar mount does not follow the shared open state then broken', () => {
	const html = (enabled) =>
		ssr.render(ssr.AgentSidebarMount, { props: { isEnabled: () => enabled } }).body;
	ssr.setOjSidebarOpen(false);
	assert.match(html(true), /aria-label="Open the assistant"/, 'closed: the edge tab shows');
	assert.doesNotMatch(html(false), /assistant-tab/, 'flag off: no tab');
	ssr.toggleOjSidebar();
	assert.doesNotMatch(html(true), /assistant-tab/, 'opened by the shared toggle: tab hides');
	ssr.setOjSidebarOpen(false);
});
