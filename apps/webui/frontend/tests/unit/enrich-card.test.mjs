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

test('a finished library reads all ready (green), with no lyrics line and no retry', () => {
	const s = summary();
	assert.ok(card.analysisLines(s).every((l) => l.tone === 'ready'));
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
	assert.match(byLane.beatgrid.text, /351 of 1,274 tracks done/);
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
	assert.equal(value.title, null, 'a reading with no song view names no denominator it does not have');
});

test('mutation control: without source counts the old native-only line still renders', () => {
	const old = summary({ beatgrid: { total: 2270, done: 16, missing: 2254 } });
	const [line] = card.analysisLines(old).filter((l) => l.lane === 'beatgrid');
	assert.equal(line.text, 'BPM and beatgrid: 16 of 2,270 tracks done, the rest running in the background');
	assert.notEqual(card.analysisLines(silver()).find((l) => l.lane === 'beatgrid').text, line.text);
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
		card.analysisLines(s).find((l) => l.lane === 'beatgrid').text,
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

/* ENRICH-03: songs, duds, the red rule and lyrics-style stems (the maintainer, Tue 6 Oct 2026). */

const songCounts = (over = {}) => ({ total: 1212, done: 1212, missing: 0, failed: 0, declined: 0, duds: 0, failed_reasons: {}, red: false, ...over });
const SONGS = { songs: 1212, files: 1998, rows: 2268, key: 'title + artists + length to the second; two rows for one file always count once', red_fail_share: 0.05 };

function songSummary(laneOver = {}, extra = {}) {
	const lanes = {};
	for (const lane of ['tags', 'strip', 'beatgrid', 'key', 'loudness', 'waveform']) {
		lanes[lane] = { total: 2268, done: 2268, missing: 0, failed: 0, declined: 0, unavailable: null, songs: songCounts(laneOver[lane]) };
	}
	return summary({}, { analysis: { lanes, songs: SONGS, duds: { files: 0, reasons: {} } }, ...extra });
}

test('lines count songs and the hover names songs in files and the key', () => {
	const s = songSummary({ loudness: { done: 275, missing: 937 } });
	const line = card.analysisLines(s).find((l) => l.lane === 'loudness');
	assert.equal(line.text, 'Loudness: 275 of 1,212 songs done, the rest running in the background');
	assert.equal(
		card.songsTitle(s),
		'1,212 songs in 1,998 files on this computer (2,268 library rows). One song = title + artists + length to the second; two rows for one file always count once.'
	);
	assert.equal(line.title, card.songsTitle(s));
});

test('control: the red flag, not a failure count, makes a lane red (4.9% ready, 5.0% red as the API reports)', () => {
	const under = songSummary({ loudness: { done: 1153, failed: 59, red: false } });
	const at = songSummary({ loudness: { done: 1151, failed: 61, red: true } });
	const lineOf = (s) => card.analysisLines(s).find((l) => l.lane === 'loudness');
	assert.equal(lineOf(under).tone, 'ready');
	assert.equal(lineOf(under).text, 'Loudness: 1,153 of 1,212 songs done, 59 failed');
	assert.equal(lineOf(at).tone, 'failed');
	assert.equal(card.offersRetry(under), false);
	assert.equal(card.needsAttention(under, null, null), false);
	assert.equal(card.offersRetry(at), true);
	assert.equal(card.needsAttention(at, null, null), true);
});

test('not analysed yet and paused are never red', () => {
	const s = songSummary({ waveform: { done: 57, missing: 1155 } });
	s.analysis.drain = { waveform: { state: 'paused_playing', waiting_on: null, reason: null } };
	const line = card.analysisLines(s).find((l) => l.lane === 'waveform');
	assert.equal(line.tone, 'working');
	assert.match(line.text, /paused while a deck is playing$/);
});

test('duds are one neutral note with reasons on hover, never a failed line', () => {
	const s = songSummary({ tags: { done: 1209, duds: 3 } });
	s.analysis.duds = { files: 5, reasons: { 'the audio cannot be decoded': 2, 'the file has no readable tags or duration': 3 } };
	assert.deepEqual(card.dudLines(s).map((l) => [l.tone, l.text]), [['note', "5 files can't be read and are skipped"]]);
	assert.match(card.dudLines(s)[0].title, /2: the audio cannot be decoded/);
	assert.ok(card.analysisLines(s).every((l) => l.tone !== 'failed'));
	assert.equal(card.dudLines(songSummary()).length, 0);
});

test('BPM over songs: under the share of none is the ready tone', () => {
	const s = songSummary({ beatgrid: { done: 16, missing: 1196 } });
	s.analysis.lanes.beatgrid.usable_songs = { denominator: 'songs', total: 1212, ready: 1160, none: 52, by_source: { rekordbox: 1158, open_dj: 2 }, allowable: true };
	const [value, note] = card.analysisLines(s).filter((l) => l.lane === 'beatgrid');
	assert.deepEqual([value.tone, value.text], ['ready', 'BPM: 1,160 of 1,212 songs ready (1,158 from rekordbox, 2 from Open DJ), 52 with none yet']);
	assert.equal(note.tone, 'note');
});

test('stems read lyrics-style from the real separated count', () => {
	assert.equal(
		card.stemsText({ state: 'ask', pending: 1222, reason: null, separated: 812, not_yet: 400 }),
		'Stems: 812 separated, 400 not yet. Separate them?'
	);
	assert.equal(
		card.stemsText({ state: 'no_source', pending: 9, reason: 'no farm', separated: 3, not_yet: 9 }),
		'Stems: 3 separated, 9 not yet, and this computer cannot make them (no farm)'
	);
});

test('lyrics count songs when the coverage carries them, and are red only on the flag', () => {
	const steps = { lyrics: { done: 700, terminal: 300, pending: 200, failed: 12, red: false } };
	const s = songSummary({}, { coverage: { ...summary().coverage, songs: { ...SONGS, steps } } });
	const line = card.lyricsLine(s);
	assert.equal(line.text, 'Lyrics: 700 found, 300 with none available, 200 still to look up, 12 failed');
	assert.equal(line.tone, 'working');
});
