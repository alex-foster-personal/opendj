/**
 * SET-6586 Routes view helpers and flag.
 *
 * [if] sets.routes_view is unread, undeclared or off [then] the Routes controls stay hidden, [else stop].
 * [if] components span slots [then] a pick, its parts and the matching set agree, [else stop].
 * [if] component names share a prefix [then] each tab glyph is still unique, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { routesViewEnabledIn, SET_ROUTES_VIEW_FLAG_ID } = await loadTypeScriptModule('src/lib/sets/routes-view-flag.ts');
const R = await loadTypeScriptModule('src/lib/components/rb/browser/set-routes.ts');
const await_tabs = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-set-tabs.ts');

const comps = [
	{ id: 1, name: 'Warm', slot: 'opener', stable_ids: ['a', 'b'], tags: [] },
	{ id: 2, name: 'Straight in', slot: 'opener', stable_ids: ['c'], tags: [] },
	{ id: 3, name: 'Main room', slot: 'main', stable_ids: ['d'], tags: [] },
	{ id: 4, name: 'Sunrise', slot: 'ending', stable_ids: ['e', 'f'], tags: [] },
	{ id: 5, name: 'Singalong', slot: 'ending', stable_ids: ['g', 'f'], tags: [] }
];

test('the Routes view shows only when the daemon declares sets.routes_view on', () => {
	assert.equal(SET_ROUTES_VIEW_FLAG_ID, 'sets.routes_view');
	assert.equal(routesViewEnabledIn([]), false, 'flags unread: off by default');
	assert.equal(routesViewEnabledIn([{ flag_id: 'sets.recording', enabled: true }]), false, 'another flag on changes nothing');
	assert.equal(routesViewEnabledIn([{ flag_id: 'sets.routes_view', enabled: false }]), false);
	assert.equal(routesViewEnabledIn([{ flag_id: 'sets.routes_view', enabled: true }]), true);
});

test('the server declares the flag off by default', () => {
	const store = readFileSync(new URL('../../../../feature_flags/store.py', import.meta.url), 'utf8');
	assert.match(store, /flag_id="sets\.routes_view",\s*default=False,/);
});

test('slots keep server order and the default pick is the first of each', () => {
	const groups = R.slotGroups(comps);
	assert.deepEqual(groups.map((g) => g.slot), ['opener', 'main', 'ending']);
	assert.deepEqual(R.partsOf(R.defaultPick(groups), groups), [1, 3, 4]);
});

test('a set route round-trips through a pick and matches only its own set', () => {
	const groups = R.slotGroups(comps);
	const pick = R.pickFromParts([2, 3, 5], comps);
	assert.deepEqual(R.partsOf(pick, groups), [2, 3, 5]);
	const sets = [
		{ id: 10, name: 'Flat', play_count: 0, parts: [] },
		{ id: 11, name: 'Early', play_count: 2, parts: [2, 3, 5] }
	];
	assert.equal(R.matchSet([2, 3, 5], sets)?.id, 11);
	assert.equal(R.matchSet([1, 3, 5], sets), null);
	assert.equal(R.matchSet([], sets), null, 'a flat set never matches an empty route');
});

test('a set that skips a slot keeps that slot off the route', () => {
	const groups = R.slotGroups(comps);
	assert.deepEqual(R.partsOf(R.pickFromParts([1, 4], comps), groups), [1, 4]);
});

test('a pick drops deleted components and an empty pick is empty, not a route', () => {
	assert.deepEqual(R.prunePick({ opener: 1, main: 99, ending: 4 }, comps), { opener: 1, ending: 4 }, 'id 99 is gone');
	assert.deepEqual(R.prunePick({}, comps), {});
	assert.deepEqual(R.pickFromParts([1, 3], []), {}, 'components not loaded yet: callers must fall back');
});

test('glyphs are the shortest unique prefix', () => {
	const g = R.componentGlyphs(comps);
	assert.deepEqual([...g.values()], ['W', 'St', 'M', 'Su', 'Si']);
	assert.equal(R.routeGlyph([1, 3, 4], g), 'W·M·Su');
	assert.equal(R.routeGlyph([99], g), '?');
	const prefix = R.componentGlyphs([
		{ id: 1, name: 'Peak', slot: 'peak', stable_ids: ['x'], tags: [] },
		{ id: 2, name: 'Peak two', slot: 'peak', stable_ids: ['y'], tags: [] }
	]);
	assert.notEqual(prefix.get(1), prefix.get(2));
});

test('shared tracks, durations and tags', () => {
	assert.deepEqual([...R.sharedTracks(comps)], ['f']);
	const len = R.tracksDuration(['a', 'b', 'zz'], (sid) => ({ a: 60_000, b: 90_500 })[sid] ?? null);
	assert.deepEqual(len, { ms: 150_500, unknown: 1 });
	assert.equal(R.formatDuration(150_500), '2:31');
	assert.equal(R.formatDuration(3_725_000), '1:02:05');
	assert.deepEqual(R.parseTags('#sunrise, outdoor #sunrise  ##late'), ['sunrise', 'outdoor', 'late']);
});

test('the browser swaps the track table for the route list only in Routes mode', () => {
	const panel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
	assert.match(
		panel,
		/setRoutesView\.active && setRoutesView\.playlistId === pane\.playlist_id\}\s*\{#await loadComponentRouteList\(\) then routeList\}\s*<routeList\.default/
	);
	const tabs = readFileSync(new URL('../../src/lib/components/rb/browser/PlaylistSetTabs.svelte', import.meta.url), 'utf8');
	assert.match(tabs, /\{#if routesOn\}\s*\{#await loadSetRoutesTools\(\) then tools\}\s*<tools\.default/);
	assert.doesNotMatch(tabs, /class="routes-toggle"/, 'the toggle lives in the lazy tools, not the eager tabs');
	const tools = readFileSync(new URL('../../src/lib/components/rb/browser/SetRoutesTools.svelte', import.meta.url), 'utf8');
	assert.match(tools, /class="routes-toggle"/);
});

test('a remote change to the selected set rebuilds the lit route before Move can overwrite it', () => {
	const tabs = readFileSync(new URL('../../src/lib/components/rb/browser/PlaylistSetTabs.svelte', import.meta.url), 'utf8');
	// Bind the call site in refresh(), not the helper declaration: a bare match on the helper passed with the call removed.
	assert.match(
		tabs,
		/const after = sets\.find\(\(s\) => s\.id === selectedId\);\s*if \(routesOn && before && after\) void repickIfChanged\(before, after\);/
	);
	assert.match(
		tabs,
		/async function repickIfChanged\(before: PlaylistSet, after: PlaylistSet\): Promise<void> \{[\s\S]*?if \(!sameParts\(before\.parts, after\.parts\)\)[\s\S]*?await pickFor\(after\)/
	);
	const moved = [1, 3, 5];
	const remote = [1, 3, 4];
	assert.equal(R.sameParts(moved, remote), false, 'a changed set is detected');
	assert.equal(R.sameParts(moved, [...moved]), true, 'an unchanged set is not repicked');
});

test('Routes opens on the most-played saved route', () => {
	const groups = R.slotGroups(comps);
	const sets = [
		{ id: 10, name: 'Flat', play_count: 9, parts: [] },
		{ id: 11, name: 'Early', play_count: 1, parts: [2, 3, 5] },
		{ id: 12, name: 'Peak', play_count: 3, parts: [1, 3, 5] }
	];
	assert.deepEqual(R.partsOf(R.openingPick(groups, comps, sets), groups), [1, 3, 5]);
	assert.deepEqual(R.partsOf(R.openingPick(groups, comps, []), groups), [1, 3, 4], 'no saved route: first of each slot');
});

test('the tabs follow only a new route-side selection of a set that still exists, never a newer tab choice', () => {
	const { routeSetToFollow } = await_tabs;
	assert.equal(routeSetToFollow(2, 1, 1, [1, 2]), 2, 'saving a route as set 2 moves the tabs to it');
	assert.equal(routeSetToFollow(2, 2, 1, [1, 2]), null, 'an unchanged route side does not move the tabs');
	assert.equal(routeSetToFollow(1, 1, 3, [1, 2, 3]), null, 'a tab choice made after the route side is kept');
	assert.equal(routeSetToFollow(2, 1, 2, [1, 2]), null, 'already in step');
	assert.equal(routeSetToFollow(9, 1, 1, [1, 2]), null, 'a route-side set that no longer exists is not followed');
	assert.equal(routeSetToFollow(null, 1, 1, [1, 2]), null);
});
