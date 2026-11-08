/**
 * UX-RAIL-01 (board #5638 W17, the maintainer Tue 6 Oct 2026): "The LHS vertical ribbon
 * has a load of icons that don't do anything - review which are a todo (for
 * later) vs meaningless now eg devices/itunes/collection should go, there's
 * also I think no icon for collection that works that brings us back to
 * collections, should be the top left".
 *
 * WHAT A PASS HERE DOES NOT COVER: SSR only (no DOM), so no click is
 * dispatched. The click path is pinned at the source level below and proven
 * live by the PR's before/after screenshots.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { default as IconRail } from '$lib/components/rb/browser/IconRail.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** The only rail entries with a real planned feature behind them (PARITY-TODO). */
const TODO_IDS = ['play-history'];

let mod;
let plannedTitle;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	({ plannedTitle } = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts'));
});

function renderRail(source = 'collection') {
	return mod.render(mod.IconRail, {
		props: { source, onspotify: () => {}, oncollection: () => {} }
	}).body;
}

const buttons = (html) => [...html.matchAll(/<button\b[^>]*>/g)].map((m) => m[0]);
const isDisabled = (tag) => /\sdisabled(?:\s*=|(?=[\s/>]))/.test(tag);
const titleOf = (tag) => /title="([^"]*)"/.exec(tag)?.[1] ?? '';
const unescape = (s) => s.replaceAll('&amp;', '&').replaceAll('&quot;', '"').replaceAll('&#39;', "'");

test('the rail renders at least one button, so the checks below have a subject', () => {
	assert.ok(buttons(renderRail()).length >= 2);
});

test('the top-left rail entry is a live way back to the whole collection', () => {
	for (const source of ['collection', 'spotify']) {
		const first = buttons(renderRail(source))[0];
		assert.match(first, /data-testid="rail-collection-home"/, `${source}: first button is the home entry`);
		assert.equal(isDisabled(first), false, `${source}: home entry is clickable`);
		assert.match(titleOf(first), /^Collection - /, `${source}: home tooltip names the collection`);
	}
});

test('every inert rail entry is a planned feature, and only the planned ones are inert', () => {
	const inertTitles = buttons(renderRail())
		.filter(isDisabled)
		.map((tag) => unescape(titleOf(tag)))
		.sort();
	assert.deepEqual(inertTitles, TODO_IDS.map((id) => plannedTitle(id)).sort());
});

test('the dead glyphs and the Collection / iTunes / Devices text tabs are gone', () => {
	const html = renderRail();
	assert.doesNotMatch(html, /rail-label/);
	assert.doesNotMatch(html, />\s*(iTunes|Devices)\s*</);
	for (const dead of ['collection-bookmarks', 'explorer-grid', 'itunes-library', 'file-browser', 'beatport', 'video-output', 'cloud-lock']) {
		assert.ok(!html.includes(unescape(plannedTitle(dead)).slice(0, 30)), `${dead} glyph still rendered`);
	}
});

test('the home entry goes to All Tracks through the agent command, and flips the tree back from Spotify', () => {
	const rail = readFileSync('src/lib/components/rb/browser/PerformanceRecorderRail.svelte', 'utf8');
	assert.match(rail, /showWholeCollectionFromChrome\(event\.timeStamp\)/);
	const chrome = readFileSync('src/lib/rb/playlist-history-chrome.svelte.ts', 'utf8');
	assert.match(chrome, /export function showWholeCollectionFromChrome[\s\S]*?type: 'browser_select_playlist', playlist_id: 'all'/);
	assert.match(rail, /if \(source !== 'collection'\) oncollection\?\.\(\);/);
	assert.match(rail, /oncollection=\{showWholeCollection\}/);
	const panel = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
	assert.match(panel, /<PerformanceRecorderRail[^>]*oncollection=\{selectCollectionSource\}/);
});

test('REC stays on the rail (the maintainer, Tue 6 Oct 2026: "important and useful to be there")', () => {
	const rail = readFileSync('src/lib/components/rb/browser/IconRail.svelte', 'utf8');
	assert.match(rail, /action: 'record'/);
	assert.match(rail, /class:recording=\{isRecord && recording\}/);
});

// REQ: USBEXP-9746
test('the USB glyph is live, opens the USB export screen, and says what the bottom bar says', () => {
	const usb = buttons(renderRail()).find((tag) => tag.includes('data-testid="rail-usb-export"'));
	assert.ok(usb, 'the rail renders the USB entry');
	assert.equal(isDisabled(usb), false, 'if the USB glyph is still inert then broken');
	const panel = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
	const bottomBarTitle = /data-testid="bottom-bar-export-eject"\s+title="([^"]+)"/.exec(panel)?.[1];
	assert.ok(bottomBarTitle, 'the bottom bar export/eject entry still exists');
	assert.equal(unescape(titleOf(usb)), bottomBarTitle, 'the rail tooltip is the built wording');
	const recorderRail = readFileSync('src/lib/components/rb/browser/PerformanceRecorderRail.svelte', 'utf8');
	assert.match(recorderRail, /onusb=\{openUsbExport\}/, 'the rail calls the same open path as the bottom bar');
	assert.match(panel, /onclick=\{openUsbExport\}/, 'the bottom bar opens it through openUsbExport too');
	assert.throws(() => plannedTitle('usb-export'), /no entry/, 'usb-export is no longer catalogued as planned');
});
