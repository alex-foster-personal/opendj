/** ENRICH-01: the enrich-on-open card says each lane state differently. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const card = await loadTypeScriptModule('src/lib/enrich/enrich-card.ts');

const full = { total: 1274, done: 1274, missing: 0, failed: 0, declined: 0, unavailable: null };

function summary(lanes = {}, extra = {}) {
	const all = {};
	for (const lane of ['tags', 'strip', 'beatgrid', 'key', 'loudness', 'waveform']) {
		all[lane] = { ...full, ...(lanes[lane] ?? {}) };
	}
	return {
		show: true,
		analysis: { lanes: all },
		analysis_error: null,
		coverage: {
			on_disk: 1274,
			done: { stems: 0, lyrics: 1274 },
			terminal: { stems: 0, lyrics: 0 },
			failed: { stems: 0, lyrics: 0 },
			pending: { stems: 0, lyrics: 0 }
		},
		coverage_error: null,
		stems: { state: 'done', pending: 0, reason: null },
		decisions: {},
		...extra
	};
}

test('a finished library has no lines and offers no retry', () => {
	const s = summary();
	assert.deepEqual(card.analysisLines(s), []);
	assert.equal(card.lyricsLine(s), null);
	assert.equal(card.offersRetry(s), false);
});

test('queued, failed and unavailable read differently', () => {
	const s = summary({
		beatgrid: { done: 351, missing: 923 },
		key: { done: 40, failed: 4, missing: 1230, failed_reasons: { 'LaneContractError: beat past end': 4 } },
		loudness: { done: 196, missing: 1078, unavailable: 'ffmpeg lacks astats' }
	});
	const lines = card.analysisLines(s);
	const byLane = Object.fromEntries(lines.map((l) => [l.lane, l]));
	assert.equal(byLane.beatgrid.tone, 'working');
	assert.match(byLane.beatgrid.text, /351 of 1,274 done/);
	assert.equal(byLane.key.tone, 'failed');
	assert.equal(byLane.key.title, '4: LaneContractError: beat past end');
	assert.equal(byLane.loudness.tone, 'unavailable');
	assert.match(byLane.loudness.text, /cannot produce it \(ffmpeg lacks astats\)/);
	assert.equal(card.offersRetry(s), true);
});

test('declined keys are counted apart and are not a failure', () => {
	const s = summary({ key: { done: 30, declined: 10, missing: 1234, declined_reasons: { no_tonal_center: 10 } } });
	const lines = card.analysisLines(s).filter((l) => l.lane === 'key');
	assert.deepEqual(lines.map((l) => l.tone), ['working', 'note']);
	assert.equal(card.offersRetry(s), false);
});

test('lyrics progress names found, none available and still to look up', () => {
	const s = summary({}, {
		coverage: {
			on_disk: 1274,
			done: { stems: 0, lyrics: 113 },
			terminal: { stems: 0, lyrics: 206 },
			failed: { stems: 0, lyrics: 0 },
			pending: { stems: 0, lyrics: 955 }
		}
	});
	assert.equal(card.lyricsLine(s).text, 'Lyrics: 113 found, 206 with none available, 955 still to look up');
});

test('stems: asked, impossible, declined and unknown each have their own wording', () => {
	assert.equal(card.stemsText({ state: 'ask', pending: 1150, reason: null }), '1,150 tracks have no stems yet. Separate them?');
	assert.match(card.stemsText({ state: 'no_source', pending: 3, reason: 'no farm' }), /cannot make them \(no farm\)/);
	assert.equal(card.stemsText({ state: 'user_declined', pending: 3, reason: null }), null);
	assert.match(card.stemsText({ state: 'unknown', pending: null, reason: 'read failed' }), /unknown: read failed/);
});

test('an unarmed drain is said, never shown as complete', () => {
	const s = summary({}, { analysis: null, analysis_error: 'drain not running' });
	assert.deepEqual(card.analysisLines(s).map((l) => l.tone), ['unavailable']);
});

test('one declined track reads in the singular', () => {
	const s = summary({ key: { done: 8, declined: 1, missing: 1265 } });
	const note = card.analysisLines(s).find((l) => l.tone === 'note');
	assert.equal(note.text, 'Key: 1 had no confident answer and is left blank');
});

test('background progress alone opens collapsed and names the running lanes', () => {
	const s = summary({ key: { done: 44, missing: 1230 }, loudness: { done: 196, missing: 1078 } });
	assert.equal(card.needsAttention(s, null, null), false);
	assert.deepEqual(card.collapsedLine(s), {
		text: 'Library: 2 lanes still running',
		title: 'Running in the background: Key, Loudness. More shows the counts.'
	});
	const one = summary({ key: { done: 44, missing: 1230 } });
	assert.equal(card.collapsedLine(one).text, 'Library: 1 lane still running');
});

test('a question, a failure, a lane that cannot run or an unknown status opens expanded', () => {
	assert.equal(card.needsAttention(summary({}, { stems: { state: 'ask', pending: 9, reason: null } }), null, null), true);
	assert.equal(card.needsAttention(summary({ strip: { done: 1271, failed: 3 } }), null, null), true);
	assert.equal(card.needsAttention(summary({ key: { unavailable: 'no model' } }), null, null), true);
	assert.equal(card.needsAttention(null, 'GET failed', null), true);
	assert.equal(card.needsAttention(summary(), null, 'PUT failed'), true);
});

test('a declined or impossible stems lane does not by itself force the card open', () => {
	const declined = summary({ key: { done: 44, missing: 1230 } }, { stems: { state: 'user_declined', pending: 9, reason: null } });
	const noSource = summary({}, { stems: { state: 'no_source', pending: 9, reason: 'no farm' } });
	assert.equal(card.needsAttention(declined, null, null), false);
	assert.equal(card.needsAttention(noSource, null, null), false);
	assert.equal(card.collapsedLine(noSource).text, 'Library: nothing left running');
});

/* ENRICH-02: honest sources, drain states and absent files (the maintainer's silver library, Tue 6 Oct 2026). */

const silverUsable = (ready, bySource) => ({ denominator: 'present', total: 2270, ready, none: 2270 - ready, by_source: bySource });

function silver(drainState = 'paused_playing', extra = {}) {
	const lane = (done, more = {}) => ({ total: 2270, done, missing: 2270 - done, failed: 0, declined: 0, unavailable: null, ...more });
	return summary(
		{},
		{
			analysis: {
				lanes: {
					tags: { total: 2270, done: 2270, missing: 0, failed: 0 },
					strip: { total: 28, done: 28, missing: 0, failed: 0 },
					loudness: lane(275),
					waveform: lane(57),
					beatgrid: lane(16, { usable: silverUsable(2270, { rekordbox: 2254, open_dj: 16 }) }),
					key: lane(19, { usable: silverUsable(2243, { rekordbox: 2243 }) })
				},
				drain: Object.fromEntries(
					['loudness', 'waveform', 'beatgrid', 'key'].map((l) => [l, { state: drainState, waiting_on: null, reason: null }])
				)
			},
			...extra
		}
	);
}

test('BPM reads as ready from rekordbox, with Open DJ re-analysis as a dim second line', () => {
	const lines = card.analysisLines(silver()).filter((l) => l.lane === 'beatgrid');
	assert.deepEqual(
		lines.map((l) => [l.tone, l.text]),
		[
			['ready', 'BPM: 2,270 of 2,270 tracks ready (2,254 from rekordbox, 16 from Open DJ)'],
			['note', 'Open DJ beatgrid re-analysis: 16 of 2,270, paused while a deck is playing']
		]
	);
});

test('a key gap is said as a count with its denominator, still not red', () => {
	const [value] = card.analysisLines(silver()).filter((l) => l.lane === 'key');
	assert.equal(value.tone, 'working');
	assert.equal(value.text, 'Key: 2,243 of 2,270 tracks ready (2,243 from rekordbox), 27 with none yet');
	assert.equal(value.title, 'Counted over the 2,270 tracks whose audio is on this computer');
});

test('mutation control: without source counts the old native-only line still renders', () => {
	const old = summary({ beatgrid: { total: 2270, done: 16, missing: 2254 } });
	const [line] = card.analysisLines(old).filter((l) => l.lane === 'beatgrid');
	assert.equal(line.text, 'BPM and beatgrid: 16 of 2,270 done, the rest running in the background');
	assert.notEqual(card.analysisLines(silver())[0].text, line.text);
});

test('each drain state reads as its own phrase', () => {
	const phrase = (state, waiting_on = null, reason = null) => card.drainPhrase({ state, waiting_on, reason });
	assert.equal(phrase('running'), 'the rest running in the background');
	assert.equal(phrase('paused_playing'), 'paused while a deck is playing');
	assert.equal(phrase('waiting', 'waveform'), 'waiting for deck waveforms to finish first');
	assert.equal(phrase('stalled', null, 'timeout:plan'), 'stalled (timeout:plan)');
	assert.equal(phrase('starting'), 'starting');
	assert.equal(phrase('done'), 'done');
	assert.equal(phrase('unavailable', null, 'no model'), 'cannot run on this computer (no model)');
	assert.equal(card.drainPhrase(undefined), 'the rest running in the background');
});

test('a lane waiting on an earlier lane says which one', () => {
	const s = silver('running');
	s.analysis.drain.key = { state: 'waiting', waiting_on: 'waveform', reason: null };
	const note = card.analysisLines(s).find((l) => l.lane === 'key' && l.tone === 'note');
	assert.equal(note.text, 'Open DJ key re-analysis: 19 of 2,270, waiting for deck waveforms to finish first');
});

test('native failures on a lane rekordbox covers are a note, and Retry is still offered', () => {
	const s = silver();
	Object.assign(s.analysis.lanes.beatgrid, { failed: 2, missing: 2252, failed_reasons: { 'TrackUnreadable: x': 2 } });
	const lines = card.analysisLines(s).filter((l) => l.lane === 'beatgrid');
	assert.deepEqual(lines.map((l) => l.tone), ['ready', 'note']);
	assert.match(lines[1].text, /, 2 could not be read$/);
	assert.equal(card.offersRetry(s), true);
	assert.equal(card.needsAttention(s, null, null), false);
});

test('a source mix lists every source, largest first as the API ranks it', () => {
	const s = silver();
	s.analysis.lanes.beatgrid.usable = silverUsable(2270, { rekordbox: 2100, inferred: 98, mik: 50, open_dj: 22 });
	assert.equal(
		card.analysisLines(s)[0].text,
		'BPM: 2,270 of 2,270 tracks ready (2,100 from rekordbox, 98 inferred from file tags, 50 from Mixed In Key, 22 from Open DJ)'
	);
});

test('files not on this Mac get their own lines with the top folders, never a percentage', () => {
	const s = silver('running', {
		coverage: {
			on_disk: 2270,
			availability: { total: 11165, present: 2270, broken_here: 0, off_machine: 8496, awaiting_volume: 11, streaming: 387, pathless: 1 },
			absent_folders: [
				{ folder: '~/Documents/TuneFab Spotify Music Converter', tracks: 2952 },
				{ folder: '~/Music/Convert', tracks: 2800 },
				{ folder: '/Users/dev/Music/Music', tracks: 854 },
				{ folder: '/Users/dev/Documents/Documents - Air', tracks: 665 }
			],
			done: { stems: 0, lyrics: 0 },
			terminal: { stems: 0, lyrics: 0 },
			failed: { stems: 0, lyrics: 0 },
			pending: { stems: 0, lyrics: 0 }
		}
	});
	const lines = card.absentLines(s);
	assert.deepEqual(
		lines.map((l) => l.text),
		[
			"8,496 tracks point at files that aren't on this Mac",
			'Most are in ~/Documents/TuneFab Spotify Music Converter (2,952), ~/Music/Convert (2,800), /Users/dev/Music/Music (854)',
			'11 tracks are on a drive that is not plugged in'
		]
	);
	assert.ok(lines.every((l) => l.tone === 'note' && !/%/.test(l.text)));
	assert.match(lines[1].title, /665: \/Users\/dev\/Documents\/Documents - Air/);
	assert.deepEqual(card.absentLines(summary()), []);
});

test('collapsed: paused lanes say paused, and BPM fully covered by rekordbox is not running', () => {
	assert.deepEqual(card.collapsedLine(silver()), {
		text: 'Library: 3 lanes paused while a deck is playing',
		title: 'Paused while a deck is playing: Key, Loudness, Deck waveforms. More shows the counts.'
	});
	assert.equal(card.collapsedLine(silver('running')).text, 'Library: 3 lanes still running');
});
