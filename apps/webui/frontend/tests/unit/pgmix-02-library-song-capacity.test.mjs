// requirement: PGMIX-7692b
// Mixer column art and Play when the library pane is tall (Preview fb4489fc78, pin a00900579b7b).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let cap;

before(async () => {
	cap = await loadTypeScriptModule('src/lib/rb/library-song-capacity.svelte.ts');
});

test('column art turns on above 20 song rows and off only at 12 or fewer', () => {
	const state = cap.librarySongCapacity;
	cap.noteLibrarySongCapacity(0);
	assert.equal(state.showMixerColumnArt, false);
	cap.noteLibrarySongCapacity(20);
	assert.equal(state.showMixerColumnArt, false, '20 rows is not above 20');
	cap.noteLibrarySongCapacity(21);
	assert.equal(state.showMixerColumnArt, true, '21 rows turns the art on');
	// The deck row grows by MIXER_COLUMN_ART_PX, which shortens the table:
	// that shrink must not turn the art straight back off.
	cap.noteLibrarySongCapacity(15);
	assert.equal(state.showMixerColumnArt, true, 'hysteresis keeps it on at 15');
	cap.noteLibrarySongCapacity(12);
	assert.equal(state.showMixerColumnArt, false, '12 rows turns it off');
	cap.noteLibrarySongCapacity(18.9);
	assert.equal(state.songs, 18);
	assert.equal(state.showMixerColumnArt, false, 'off stays off until above 20');
});

test('a non-finite or negative capacity is ignored', () => {
	cap.noteLibrarySongCapacity(30);
	assert.equal(cap.librarySongCapacity.showMixerColumnArt, true);
	cap.noteLibrarySongCapacity(Number.NaN);
	cap.noteLibrarySongCapacity(-1);
	assert.equal(cap.librarySongCapacity.songs, 30);
	assert.equal(cap.librarySongCapacity.showMixerColumnArt, true);
});

test('the art height matches the deck row growth on the page', () => {
	assert.equal(cap.MIXER_COLUMN_ART_PX, 64);
	const page = readFrontendSource('src/routes/performance/+page.svelte');
	assert.match(
		page,
		/--mixer-col-art-extra: \$\{librarySongCapacity\.showMixerColumnArt \? MIXER_COLUMN_ART_PX : 0\}px/
	);
	const mixer = readFrontendSource('src/lib/components/rb/Mixer.svelte');
	assert.match(mixer, /data-testid=\{`mixer-play-deck-\$\{deck\}`\}/);
	assert.match(mixer, /type: 'play',/);
	const table = readFrontendSource('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /onlibraryviewport\?\.\(viewportHeight - TRACK_TABLE_THEAD_PX, rowHeight\)/);
	const panel = readFrontendSource('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /onlibraryviewport=\{noteLibraryViewport\}/);
});

// TOPVIEW-01: a view switch never resizes the deck row. Measured at 1280x970
// in LESS: the table viewport below its header is 431px in waveform view and
// 543px in List view (the 112px waveform row moved to the library). At 22px
// rows that is 19 rows against 24, so counting the List view height would
// turn the art on, grow the deck row, and break TOPVIEW-01.
// The count settles in a timer task, so each step waits one task, as the page does.
const nextTask = () => new Promise((resolve) => setTimeout(resolve, 0));

test('List view counts library rows as in waveform view', async () => {
	const state = cap.librarySongCapacity;
	cap.noteLibrarySongCapacity(0);
	cap.noteHiddenWaveRowPx(0);
	cap.noteLibraryViewport(431, 22);
	await nextTask();
	assert.equal(state.songs, 19, 'waveform view: 431px holds 19 rows');
	assert.equal(state.showMixerColumnArt, false);

	cap.noteHiddenWaveRowPx(112);
	cap.noteLibraryViewport(543, 22);
	await nextTask();
	assert.equal(state.songs, 19, 'List view: the same 19 rows, not the 24 its viewport holds');
	assert.equal(state.showMixerColumnArt, false, 'List view alone does not turn the art on');

	// A window tall enough in waveform view gets the art in both views.
	cap.noteHiddenWaveRowPx(0);
	cap.noteLibraryViewport(500, 22);
	await nextTask();
	assert.equal(state.showMixerColumnArt, true, 'waveform view: 22 rows turns the art on');
	cap.noteHiddenWaveRowPx(112);
	cap.noteLibraryViewport(612, 22);
	await nextTask();
	assert.equal(state.songs, 22, 'List view keeps the waveform view count');
	assert.equal(state.showMixerColumnArt, true);

	cap.noteHiddenWaveRowPx(0);
	cap.noteLibraryViewport(0, 0);
	await nextTask();
	assert.equal(state.showMixerColumnArt, false, 'an unmounted table turns the art off');
});

test('the page reports the collapsed waveform row from the wavestack track variables', () => {
	const page = readFrontendSource('src/routes/performance/+page.svelte');
	assert.match(page, /class="wavestack-hidden-sizer"[^>]*bind:clientHeight=\{hiddenWaveRowPx\}/);
	assert.match(page, /noteHiddenWaveRowPx\(hiddenWaveRowPx\)/);
	assert.match(page, /\.wavestack-hidden-sizer \{[^}]*height: 0;/, 'waveform view hides no row');
	const rule = (cls) =>
		new RegExp(
			`\\.perf-root\\.perf-view-list${cls} \\.wavestack-hidden-sizer \\{\\s*height: calc\\(${cls ? 2 : 4} \\* \\(var\\(--rb-waverow-h\\) \\+ var\\(--rb-stemwave-stack-extra, 0px\\)\\)\\);`
		);
	assert.match(page, rule(''), 'MORE List view hides the four-row wavestack track');
	assert.match(page, rule('.deck-layout-less'), 'LESS List view hides the two-row wavestack track');
});
