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

test('import duplicate count links to dedup when above zero', () => {
	const line = card.importDuplicateLine({ ingest_duplicates: 2 });
	assert.equal(line?.href, '/dedup');
	assert.match(line?.text ?? '', /2 imported files duplicate tracks already in the library/);
	assert.equal(card.importDuplicateLine({ ingest_duplicates: 0 }), null);
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

test('background progress alone leaves the pill unflagged and names the running lanes', () => {
	const s = summary({ key: { done: 44, missing: 1230 }, loudness: { done: 196, missing: 1078 } });
	assert.equal(card.needsAttention(s, null, null), false);
	assert.deepEqual(card.collapsedLine(s), {
		text: 'Library: 2 lanes still running',
		title: 'Running in the background: Key, Loudness. Click for the counts.'
	});
	const one = summary({ key: { done: 44, missing: 1230 } });
	assert.equal(card.collapsedLine(one).text, 'Library: 1 lane still running');
});

test('a question, a failure, a lane that cannot run or an unknown status flags the pill (never auto-expands, #5748)', () => {
	assert.equal(card.needsAttention(summary({}, { stems: { state: 'ask', pending: 9, reason: null } }), null, null), true);
	assert.equal(card.needsAttention(summary({ strip: { done: 1271, failed: 3 } }), null, null), true);
	assert.equal(card.needsAttention(summary({ key: { unavailable: 'no model' } }), null, null), true);
	assert.equal(card.needsAttention(null, 'GET failed', null), true);
	assert.equal(card.needsAttention(summary(), null, 'PUT failed'), true);
});

test('the needs-you hover names what a person has to act on', () => {
	const asked = card.attentionTitle(summary({}, { stems: { state: 'ask', pending: 50, reason: null } }));
	assert.match(asked, /Stems still need a yes or no/);
	const failed = card.attentionTitle(summary({ strip: { done: 1271, failed: 3 } }));
	assert.match(failed, /Click to see it\.$/);
	assert.doesNotMatch(failed, /Stems still need/);
});

test('a declined or impossible stems lane does not by itself flag the pill', () => {
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
		title: 'Paused while a deck is playing: Key, Loudness, Deck waveforms. Click for the counts.'
	});
	assert.equal(card.collapsedLine(silver('running')).text, 'Library: 3 lanes still running');
});

/* ENRICH-03: songs, duds, the red rule and lyrics-style stems (the maintainer, Tue 6 Oct 2026). */

const songCounts = (over = {}) => ({
	total: 1212, done: 1212, missing: 0, failed: 0, declined: 0, duds: 0, failed_reasons: {}, red: false,
	allowable: (over.missing ?? 0) + (over.failed ?? 0) === 0,
	...over
});
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
	const under = songSummary({ loudness: { done: 1153, failed: 59, red: false, allowable: true } });
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

test('green when failures plus pending are allowable, and the text still names what is left', () => {
	const s = songSummary({ waveform: { done: 1160, missing: 50, failed: 2, allowable: true } });
	s.analysis.drain = { waveform: { state: 'paused_playing', waiting_on: null, reason: null } };
	const line = card.analysisLines(s).find((l) => l.lane === 'waveform');
	assert.equal(line.tone, 'ready');
	assert.equal(line.text, 'Deck waveforms: 1,160 of 1,212 songs done, paused while a deck is playing, 2 failed');
	const neutral = songSummary({ waveform: { done: 1150, missing: 60, failed: 2, allowable: false } });
	assert.equal(card.analysisLines(neutral).find((l) => l.lane === 'waveform').tone, 'working');
});

/* ENRICH-04: failed separations are never silent (the maintainer, Wed 7 Oct 2026: "very often tonnes of stems are missing and UI is doing nothing about it"). */

test('failed separations with nothing pending name the count, lyrics-style when songs are known', () => {
	assert.equal(
		card.stemsText({ state: 'failed', pending: 0, failed: 37, reason: null, separated: 812, not_yet: 0 }),
		'Stems: 812 separated, 0 not yet, 37 failed'
	);
	assert.equal(card.stemsText({ state: 'failed', pending: 0, failed: 5, reason: null }), 'Stems: 5 tracks failed to separate');
	assert.equal(card.stemsText({ state: 'failed', pending: 0, failed: 1, reason: null }), 'Stems: 1 track failed to separate');
	assert.equal(card.stemsTone({ state: 'failed', pending: 0, failed: 5, reason: null }), 'failed');
	assert.match(card.stemsTitle({ state: 'failed', pending: 0, failed: 5, reason: null }), /^5 failed: /);
});

test('failures ride along with the question and with a missing source', () => {
	assert.equal(
		card.stemsText({ state: 'ask', pending: 1222, failed: 37, reason: null, separated: 812, not_yet: 400 }),
		'Stems: 812 separated, 400 not yet, 37 failed. Separate them?'
	);
	assert.equal(
		card.stemsText({ state: 'ask', pending: 9, failed: 2, reason: null }),
		'9 tracks have no stems yet, 2 failed. Separate them?'
	);
	assert.equal(
		card.stemsText({ state: 'no_source', pending: 9, failed: 2, reason: 'no farm', separated: 3, not_yet: 9 }),
		'Stems: 3 separated, 9 not yet, 2 failed, and this computer cannot make them (no farm)'
	);
});

test('control: no failures leaves every stems wording and tone as it was', () => {
	assert.equal(card.stemsText({ state: 'done', pending: 0, failed: 0, reason: null }), null);
	assert.equal(
		card.stemsText({ state: 'ask', pending: 1222, failed: 0, reason: null, separated: 812, not_yet: 400 }),
		'Stems: 812 separated, 400 not yet. Separate them?'
	);
	assert.equal(card.stemsTitle({ state: 'ask', pending: 9, failed: 0, reason: null }), null);
	assert.equal(card.stemsTone({ state: 'ask', pending: 9, reason: null }), 'working');
	assert.equal(card.stemsTone({ state: 'no_source', pending: 9, reason: 'no farm' }), 'unavailable');
});

test('failed separations open the card expanded; a clean or declined stems lane does not', () => {
	const failed = summary({}, { stems: { state: 'failed', pending: 0, failed: 3, reason: null } });
	assert.equal(card.needsAttention(failed, null, null), true, 'failed separations must open the card');
	const noSourceFailed = summary({}, { stems: { state: 'no_source', pending: 9, failed: 3, reason: 'no farm' } });
	assert.equal(card.needsAttention(noSourceFailed, null, null), true, 'failures must not hide behind a missing source');
	// Controls: nothing failed stays collapsed; a stored never is unchanged and stays quiet.
	assert.equal(card.needsAttention(summary({}, { stems: { state: 'done', pending: 0, failed: 0, reason: null } }), null, null), false);
	const declined = summary({}, { stems: { state: 'user_declined', pending: 0, failed: 3, reason: null } });
	assert.equal(card.needsAttention(declined, null, null), false);
	assert.equal(card.stemsText(declined.stems), null);
});

/* ENRICH-04: a source refusal files missing stems as terminal, so they are named with their count, never "done". */

test('no stems source: the count that cannot be made here is named, songs when known', () => {
	assert.equal(
		card.stemsText({ state: 'no_source', pending: 0, failed: 0, missing: 1150, reason: 'no stems farm is configured', separated: 62, not_yet: 0 }),
		"Stems: 62 separated, 1,150 songs can't be made on this computer (no stems farm is configured)"
	);
	assert.equal(
		card.stemsText({ state: 'no_source', pending: 3, failed: 2, missing: 1, reason: 'no farm' }),
		"Stems: 3 not yet, 2 failed, 1 track can't be made on this computer (no farm)"
	);
	assert.equal(card.stemsTone({ state: 'no_source', pending: 0, missing: 1150, reason: 'no farm' }), 'unavailable');
	assert.match(card.stemsTitle({ state: 'no_source', pending: 0, missing: 1150, reason: 'no farm' }), /^1,150 without stems: .*\(no farm\)/);
});

test('control: a no_source line with nothing refused keeps its old wording and no title', () => {
	assert.equal(
		card.stemsText({ state: 'no_source', pending: 9, failed: 0, missing: 0, reason: 'no farm', separated: 3, not_yet: 9 }),
		'Stems: 3 separated, 9 not yet, and this computer cannot make them (no farm)'
	);
	assert.equal(card.stemsTitle({ state: 'no_source', pending: 9, failed: 0, missing: 0, reason: 'no farm' }), null);
});

test('no stems source opens collapsed and the collapsed line names it, unless the user said never', () => {
	const noSource = summary({}, { stems: { state: 'no_source', pending: 0, failed: 0, missing: 1150, reason: 'no farm' } });
	assert.equal(card.needsAttention(noSource, null, null), false, 'no answer here can change it: collapsed');
	assert.equal(card.collapsedLine(noSource).text, 'Library: 1,150 without stems, none can be made here');
	assert.match(card.collapsedLine(noSource).title, /no farm/);
	const declined = { ...noSource, decisions: { stems: 'never' } };
	assert.equal(card.collapsedLine(declined).text, 'Library: nothing left running');
	// A stored never also keeps no_source failures from forcing the card open.
	const declinedFailed = summary({}, { stems: { state: 'no_source', pending: 0, failed: 3, missing: 5, reason: 'no farm' }, decisions: { stems: 'never' } });
	assert.equal(card.needsAttention(declinedFailed, null, null), false);
	// Lanes still running keep the running-lanes line AND still name the refusal (#5855 Codex P2).
	const running = summary({ key: { done: 44, missing: 1230 } }, { stems: noSource.stems });
	assert.equal(card.collapsedLine(running).text, 'Library: 1 lane still running; 1,150 without stems, none can be made here');
	assert.match(card.collapsedLine(running).title, /no farm/);
	// Control: with a stored never, the running line carries no stems clause.
	const runningDeclined = { ...running, decisions: { stems: 'never' } };
	assert.equal(card.collapsedLine(runningDeclined).text, 'Library: 1 lane still running');
	// Control: without a refusal, the running line is unchanged.
	const runningAsk = summary({ key: { done: 44, missing: 1230 } });
	assert.doesNotMatch(card.collapsedLine(runningAsk).text, /without stems/);
});

// requirement ENRICH-05: a stored "never" is said on the card and can be taken back.
test('a stored never is named with what it leaves, and nothing is said without one', () => {
	const declined = summary({}, {
		stems: { state: 'user_declined', pending: 1150, failed: 0, reason: null },
		decisions: { stems: 'never' }
	});
	assert.equal(
		card.stemsDeclinedText(declined),
		'Stems: you chose never for this library. 1,150 tracks have no stems. A deck can still separate one track.'
	);
	assert.equal(card.stemsDeclinedText(summary({}, { decisions: { stems: 'ask' } })), null);
	assert.equal(card.stemsDeclinedText(summary()), null);
	// not_yet counts songs (ENRICH-03), so it is worded as songs, not tracks.
	const songs = summary({}, {
		stems: { state: 'user_declined', pending: 1150, failed: 0, reason: null, separated: 10, not_yet: 900 },
		decisions: { stems: 'never' }
	});
	assert.equal(
		card.stemsDeclinedText(songs),
		'Stems: you chose never for this library. 900 songs have no stems. A deck can still separate one track.'
	);
	const oneSong = summary({}, {
		stems: { state: 'user_declined', pending: 3, failed: 0, reason: null, separated: 10, not_yet: 1 },
		decisions: { stems: 'never' }
	});
	assert.match(card.stemsDeclinedText(oneSong), / 1 song has no stems\./);
	// A null not_yet beside a numeric pending is the track count, said as
	// tracks, never as songs (Sol P1 on #6819).
	const nullSongs = summary({}, {
		stems: { state: 'user_declined', pending: 1150, failed: 0, reason: null, separated: 10, not_yet: null },
		decisions: { stems: 'never' }
	});
	assert.equal(
		card.stemsDeclinedText(nullSongs),
		'Stems: you chose never for this library. 1,150 tracks have no stems. A deck can still separate one track.'
	);
});

test('a standing never keeps the pill up and collapsed, and says nothing when stems have their own line', () => {
	const declined = summary({}, {
		show: false,
		stems: { state: 'user_declined', pending: 1150, failed: 0, reason: null },
		decisions: { stems: 'never' }
	});
	assert.equal(card.keepsCardReachable(declined), true);
	assert.equal(card.needsAttention(declined, null, null), false, 'never must not nag');
	assert.equal(card.collapsedLine(declined).text, 'Library: stems set to never');

	const noSource = summary({}, {
		stems: { state: 'no_source', pending: 0, failed: 0, missing: 40, reason: 'no relay' },
		decisions: { stems: 'never' }
	});
	assert.equal(card.stemsDeclinedText(noSource), null);
	assert.equal(card.keepsCardReachable(noSource), false);
	assert.equal(card.keepsCardReachable(summary()), false, 'control: nothing declined keeps nothing up');
	assert.equal(card.keepsCardReachable(null), false);
});

test('Hide hides the card, except while a standing never keeps its undo reachable', () => {
	const declined = summary({}, {
		show: false,
		stems: { state: 'user_declined', pending: 1150, failed: 0, reason: null },
		decisions: { stems: 'never' }
	});
	assert.equal(card.cardVisible(declined, null, true), true, 'Hide must not strand "Ask about stems again"');
	const showing = summary({}, { show: true });
	assert.equal(card.cardVisible(showing, null, false), true, 'control: a card with something to say shows');
	assert.equal(card.cardVisible(showing, null, true), false, 'control: Hide still hides a card with no standing never');
	assert.equal(card.cardVisible(null, 'GET failed', true), false, 'control: Hide hides a load error too');
	assert.equal(card.cardVisible(null, 'GET failed', false), true);
	assert.equal(card.cardVisible(summary({}, { show: false }), null, false), false);
});

// Sol on #6819: a stored never with no count is a drifted summary, said so, never read as 0.
test('a stored never whose summary carries no count says the count is unknown', () => {
	const drifted = summary({}, {
		stems: { state: 'user_declined', pending: null, failed: 0, reason: null },
		decisions: { stems: 'never' }
	});
	assert.match(card.stemsDeclinedText(drifted), /How many have no stems is unknown: the summary carried neither not_yet nor pending\./);
	// Control: a real zero says nothing about a count.
	const none = summary({}, {
		stems: { state: 'user_declined', pending: 0, failed: 0, reason: null },
		decisions: { stems: 'never' }
	});
	assert.equal(card.stemsDeclinedText(none), 'Stems: you chose never for this library. A deck can still separate one track.');
});

// ENRICH-08: a lane that cannot run is said, with where to set what is missing.
const blocking = {
	lane: 'artwork',
	code: 'ARTWORK_ONLINE_OFF',
	message: 'Missing covers are not looked up online: online lookup is off.',
	fix: 'Remove ODJ_ARTWORK_ONLINE=0, then restart.',
	severity: 'blocking'
};
const info = {
	lane: 'genre',
	code: 'AI_GENRE_KEYS_MISSING',
	message: 'AI genre tagging cannot run: OPENROUTER_API_KEY not set.',
	fix: 'Set it in the environment.',
	severity: 'info'
};

test('a blocking capability warning is an unavailable line naming the fix, and the pill needs you', () => {
	const s = summary({}, { warnings: [blocking] });
	const lines = card.warningLines(s);
	assert.equal(lines.length, 1);
	assert.equal(lines[0].tone, 'unavailable');
	assert.ok(lines[0].text.includes('ODJ_ARTWORK_ONLINE=0'), 'the fix is in the line, not only on hover');
	assert.equal(lines[0].title, blocking.fix);
	assert.equal(card.needsAttention(s, null, null), true);
});

test('an info warning (opt-in AI genre) is a note and never flags the pill', () => {
	const s = summary({}, { warnings: [info] });
	assert.equal(card.warningLines(s)[0].tone, 'note');
	assert.equal(card.needsAttention(s, null, null), false);
});

test('a summary with no warnings field renders no warning lines', () => {
	assert.deepEqual(card.warningLines(summary()), []);
});

test('ingest genre fill reads filled and need-a-look counts (SETPREP-10646)', () => {
	const line = card.ingestGenreLine(summary({}, { ingest_genre: { total: 40, done: 12, needs_review: 28 } }));
	assert.equal(line.text, 'Genre: 12 of 40 filled, 28 need a look');
	assert.equal(line.tone, 'working');
	assert.equal(card.ingestGenreLine(summary({}, { ingest_genre: { total: 10, done: 10, needs_review: 0 } })).tone, 'ready');
});

test('ingest identify line (SETPREP-12099)', () => {
	assert.equal(
		card.ingestIdentifyLine(summary({}, { ingest_identify: { total: 9, proposals: 7 } })).text,
		'Identify: 7 proposals to review'
	);
	assert.equal(card.ingestIdentifyLine(summary({}, { ingest_identify: { total: 1, proposals: 1 } })).text, 'Identify: 1 proposal to review');
	assert.equal(card.ingestIdentifyLine(summary({}, { ingest_identify: { total: 5, proposals: 0 } })), null);
	assert.equal(
		card.ingestIdentifyLine(summary({}, { ingest_identify: { total: 3, proposals: 1, deferred: 2 } })).text,
		'Identify: 1 proposal to review, 2 deferred'
	);
	assert.equal(card.TITLE_FIX_HREF, '/admin?tab=title-fix');
});
