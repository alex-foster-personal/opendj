/**
 * Enrich-on-open card policy (ENRICH-01, ENRICH-02, ENRICH-03). Pure: the component fetches
 * GET /api/v1/enrich/summary and renders exactly the lines this returns, so
 * every state in specs/state-inventories/library-enrichment.md is unit-testable here.
 *
 * Counts are SONGS when the summary carries the backend's song view
 * (apps/webui/server/enrich_songs.py): rows for one file, and copies of one
 * song, count once. Every number comes from the summary; nothing is recounted
 * here, and the red rule is the backend's `red` flag, never a threshold here.
 *
 * One line per lane, and each state reads differently:
 *   ready       green (white in Gothic): complete, or failures and gaps under
 *               the red share. BPM and key say how many songs have a value
 *               from ANY source; Open DJ's own re-analysis is a dim note
 *   working     "Loudness: 275 of 1,212 songs done, paused while a deck is playing"
 *   declined    counted apart: an answer, not unfinished work
 *   failed      red ONLY at the backend's red share of real failures
 *   unavailable this computer cannot produce the lane, with the reason
 *   duds        files that cannot be read: one neutral note, never a failure
 *
 * Tracks whose files are not on this Mac get their own lines, never a
 * percentage (docs/library-availability.md, the denominator house rule).
 */

export type DrainState = {
	state: 'running' | 'paused_playing' | 'waiting' | 'stalled' | 'starting' | 'done' | 'unavailable';
	waiting_on: string | null;
	reason: string | null;
};

export type UsableCounts = {
	denominator: string;
	total: number;
	ready: number;
	none: number;
	by_source: Record<string, number>;
	/** Song view only: gaps are under the red share, so the line is not "working". */
	allowable?: boolean;
};

export type SongCounts = {
	total: number;
	done: number;
	missing: number;
	/** Real failures only: duds are counted apart. */
	failed: number;
	declined: number;
	duds: number;
	failed_reasons: Record<string, number>;
	/** Real failures at or above the backend's red share. */
	red: boolean;
	/** Real failures plus still-to-analyse are under the red share: the ready tone. */
	allowable: boolean;
};

export type LaneCounts = {
	total: number;
	done: number;
	missing: number;
	failed: number;
	declined?: number;
	failed_reasons?: Record<string, number>;
	declined_reasons?: Record<string, number>;
	unavailable?: string | null;
	/** BPM and key only: values usable from any source (ENRICH-02). */
	usable?: UsableCounts;
	/** ENRICH-03: the same lane over unique songs. */
	songs?: SongCounts;
	usable_songs?: UsableCounts;
};

export type SongsSummary = { songs: number; files: number; rows: number; key: string; red_fail_share?: number };

export type StemsLine = {
	state: 'ask' | 'done' | 'no_source' | 'user_declined' | 'unknown';
	pending: number | null;
	reason: string | null;
	/** ENRICH-03: songs with a real stem bundle, and songs still without one. */
	separated?: number;
	not_yet?: number;
};

export type AbsentFolder = { folder: string; tracks: number };

type StepSongs = { done: number; terminal: number; pending: number; failed: number; red: boolean };

export type EnrichSummary = {
	show: boolean;
	analysis: {
		lanes: Record<string, LaneCounts>;
		drain?: Record<string, DrainState>;
		songs?: SongsSummary;
		duds?: { files: number; reasons: Record<string, number> };
	} | null;
	analysis_error: string | null;
	coverage: {
		on_disk: number;
		/** Playability buckets of every library row (library_playable). */
		availability?: Record<string, number>;
		absent_folders?: AbsentFolder[];
		songs?: (SongsSummary & { steps: Record<string, StepSongs> }) | null;
		done: Record<string, number>;
		terminal: Record<string, number>;
		failed: Record<string, number>;
		pending: Record<string, number>;
	} | null;
	coverage_error: string | null;
	stems: StemsLine;
	decisions: Record<string, string>;
};

export type CardLine = {
	lane: string;
	tone: 'ready' | 'working' | 'failed' | 'unavailable' | 'note';
	text: string;
	/** Hover text: the named reasons behind a count. */
	title: string | null;
};

const LANE_LABELS: Record<string, string> = {
	tags: 'File tags',
	strip: 'Preview waveforms',
	beatgrid: 'BPM and beatgrid',
	key: 'Key',
	loudness: 'Loudness',
	waveform: 'Deck waveforms'
};

/** What the value line of a lane with library sources is called. */
const VALUE_LABELS: Record<string, string> = { beatgrid: 'BPM', key: 'Key' };

/** A lane named inside a sentence ("waiting for deck waveforms"). */
const LANE_PHRASES: Record<string, string> = {
	tags: 'file tags',
	strip: 'preview waveforms',
	beatgrid: 'beatgrids',
	key: 'keys',
	loudness: 'loudness',
	waveform: 'deck waveforms'
};

const SOURCE_PHRASES: Record<string, string> = {
	rekordbox: 'from rekordbox',
	open_dj: 'from Open DJ',
	inferred: 'inferred from file tags',
	mik: 'from Mixed In Key',
	manual: 'set by hand',
	webui: 'set by hand'
};

/** Shown on a folder-list line; the rest are on hover. */
const FOLDERS_INLINE = 3;

const n = (value: number): string => value.toLocaleString('en-US');

function reasonsTitle(reasons: Record<string, number> | undefined): string | null {
	const entries = Object.entries(reasons ?? {});
	if (entries.length === 0) return null;
	return entries.map(([why, count]) => `${n(count)}: ${why}`).join('\n');
}

/** The song view, from whichever reading carries it. */
function songsOf(summary: EnrichSummary): SongsSummary | null {
	return summary.analysis?.songs ?? summary.coverage?.songs ?? null;
}

/** Hover text naming the denominator: songs in files, and what makes two rows one song. */
export function songsTitle(summary: EnrichSummary): string | null {
	const s = songsOf(summary);
	if (s === null) return null;
	return `${n(s.songs)} songs in ${n(s.files)} files on this computer (${n(s.rows)} library rows). One song = ${s.key}.`;
}

/** What a lane is counted in, and the lane's counts in that unit. */
function view(c: LaneCounts): { noun: string; counts: SongCounts } {
	if (c.songs) return { noun: 'songs', counts: c.songs };
	return {
		noun: 'tracks',
		counts: {
			total: c.total,
			done: c.done,
			missing: c.missing,
			failed: c.failed,
			declined: c.declined ?? 0,
			duds: 0,
			failed_reasons: c.failed_reasons ?? {},
			red: c.failed > 0,
			allowable: c.missing === 0 && c.failed === 0
		}
	};
}

/** Why a lane is or is not moving, as the tail of a sentence. */
export function drainPhrase(drain: DrainState | undefined): string {
	if (drain === undefined) return 'the rest running in the background';
	switch (drain.state) {
		case 'running':
			return 'the rest running in the background';
		case 'paused_playing':
			return 'paused while a deck is playing';
		case 'waiting':
			return `waiting for ${LANE_PHRASES[drain.waiting_on ?? ''] ?? drain.waiting_on} to finish first`;
		case 'stalled':
			return `stalled (${drain.reason})`;
		case 'starting':
			return 'starting';
		case 'done':
			return 'done';
		case 'unavailable':
			return `cannot run on this computer (${drain.reason})`;
		default: {
			const exhaustive: never = drain.state;
			throw new Error(`Unhandled drain state: ${exhaustive}`);
		}
	}
}

function sourcesPhrase(bySource: Record<string, number>): string {
	return Object.entries(bySource)
		.map(([source, count]) => `${n(count)} ${SOURCE_PHRASES[source] ?? `from ${source}`}`)
		.join(', ');
}

const allowedFailures = (failed: number): string => (failed > 0 ? `, ${n(failed)} failed` : '');

/** BPM or key: ready from any source, then Open DJ's own re-analysis as a dim note. */
function valueLines(lane: string, c: LaneCounts, title: string | null, drain: DrainState | undefined): CardLine[] {
	const usable = (c.usable_songs ?? c.usable) as UsableCounts;
	const { noun, counts } = view(c);
	const sources = sourcesPhrase(usable.by_source);
	const settled = usable.allowable ?? usable.none === 0;
	const lines: CardLine[] = [
		{
			lane,
			tone: settled ? 'ready' : 'working',
			text:
				`${VALUE_LABELS[lane]}: ${n(usable.ready)} of ${n(usable.total)} ${noun} ready` +
				(sources ? ` (${sources})` : '') +
				(usable.none > 0 ? `, ${n(usable.none)} with none yet` : ''),
			title
		}
	];
	if (counts.missing > 0 || counts.failed > 0) {
		lines.push({
			lane,
			tone: c.songs?.red ? 'failed' : 'note',
			text:
				`Open DJ ${LANE_PHRASES[lane].replace(/s$/, '')} re-analysis: ${n(counts.done)} of ${n(counts.total)}, ` +
				(counts.missing > 0 ? drainPhrase(drain) : 'finished') +
				(counts.failed > 0 ? `, ${n(counts.failed)} could not be read` : '') +
				(counts.declined > 0 ? `, ${n(counts.declined)} with no confident answer` : ''),
			title: reasonsTitle(counts.failed_reasons) ?? title
		});
	}
	return lines;
}

/** One automatic lane: red only on the backend's red flag; complete reads green. */
function laneLine(lane: string, label: string, c: LaneCounts, title: string | null, drain: DrainState | undefined): CardLine {
	const { noun, counts } = view(c);
	if (counts.red) {
		return {
			lane,
			tone: 'failed',
			text: `${label}: ${n(counts.failed)} of ${n(counts.total)} ${noun} failed, ${n(counts.done)} done`,
			title: reasonsTitle(counts.failed_reasons)
		};
	}
	const progress = `${label}: ${n(counts.done)} of ${n(counts.total)} ${noun} done`;
	const remaining = counts.missing > 0 ? `, ${drainPhrase(drain)}` : '';
	// Failures plus still-to-analyse under the backend's red share is allowable: green, and the
	// text still names what is left, so green never hides undone work.
	const tone = counts.allowable ? 'ready' : 'working';
	return { lane, tone, text: `${progress}${remaining}${allowedFailures(counts.failed)}`, title: reasonsTitle(counts.failed_reasons) ?? title };
}

/** The analysis lane lines, in the drain's order. */
export function analysisLines(summary: EnrichSummary): CardLine[] {
	if (summary.analysis === null) {
		return summary.analysis_error
			? [{ lane: 'analysis', tone: 'unavailable', text: `Analysis status unknown: ${summary.analysis_error}`, title: null }]
			: [];
	}
	const title = songsTitle(summary);
	const lines: CardLine[] = [];
	for (const [lane, label] of Object.entries(LANE_LABELS)) {
		const c = summary.analysis.lanes[lane];
		if (!c) continue;
		const drain = summary.analysis.drain?.[lane];
		if ((c.usable_songs ?? c.usable) && VALUE_LABELS[lane]) {
			lines.push(...valueLines(lane, c, title, drain));
			if (c.unavailable) {
				lines.push({ lane, tone: 'unavailable', text: `${label}: this computer cannot re-analyse it (${c.unavailable})`, title: null });
			}
			continue;
		}
		if (c.unavailable) {
			lines.push({ lane, tone: 'unavailable', text: `${label}: this computer cannot produce it (${c.unavailable})`, title: null });
			continue;
		}
		lines.push(laneLine(lane, label, c, title, drain));
		const declined = view(c).counts.declined;
		if (declined > 0 && (c.missing > 0 || c.failed > 0)) {
			lines.push({
				lane,
				tone: 'note',
				text: `${label}: ${n(declined)} had no confident answer and ${declined === 1 ? 'is' : 'are'} left blank`,
				title: reasonsTitle(c.declined_reasons)
			});
		}
	}
	return lines;
}

/** Files no lane can read: one neutral note, never a failure. */
export function dudLines(summary: EnrichSummary): CardLine[] {
	const duds = summary.analysis?.duds;
	if (!duds || duds.files === 0) return [];
	return [
		{
			lane: 'duds',
			tone: 'note',
			text: `${n(duds.files)} ${duds.files === 1 ? "file can't be read and is" : "files can't be read and are"} skipped`,
			title: reasonsTitle(duds.reasons)
		}
	];
}

/** The lyrics line: automatic, so progress only. */
export function lyricsLine(summary: EnrichSummary): CardLine | null {
	const c = summary.coverage;
	if (c === null) {
		return summary.coverage_error
			? { lane: 'lyrics', tone: 'unavailable', text: `Lyrics status unknown: ${summary.coverage_error}`, title: null }
			: null;
	}
	const songs = c.songs?.steps.lyrics;
	const pending = songs ? songs.pending : (c.pending.lyrics ?? 0);
	const failed = songs ? songs.failed : (c.failed.lyrics ?? 0);
	if (pending === 0 && failed === 0) return null;
	const found = songs ? songs.done : (c.done.lyrics ?? 0);
	const none = songs ? songs.terminal : (c.terminal.lyrics ?? 0);
	const red = songs ? songs.red : failed > 0;
	return {
		lane: 'lyrics',
		tone: red ? 'failed' : 'working',
		text:
			`Lyrics: ${n(found)} found, ${n(none)} with none available, ${n(pending)} still to look up` +
			(failed > 0 ? `, ${n(failed)} failed` : ''),
		title: songsTitle(summary)
	};
}

/**
 * Library rows whose audio is not on this Mac: a count and the folders they
 * point into, never folded into a percentage. Unplugged drives are said apart,
 * because the audio is presumed fine there.
 */
export function absentLines(summary: EnrichSummary): CardLine[] {
	const availability = summary.coverage?.availability;
	if (!availability) return [];
	const folders = summary.coverage?.absent_folders ?? [];
	const absent = (availability.off_machine ?? 0) + (availability.broken_here ?? 0);
	const unplugged = availability.awaiting_volume ?? 0;
	const lines: CardLine[] = [];
	if (absent > 0) {
		const brokenHere = availability.broken_here ?? 0;
		lines.push({
			lane: 'absent',
			tone: 'note',
			text:
				`${n(absent)} ${absent === 1 ? 'track points' : 'tracks point'} at files that aren't on this Mac` +
				(brokenHere > 0 ? ` (${n(brokenHere)} of them were here before)` : ''),
			title: `Of all ${n(availability.total ?? 0)} library rows. They are not counted in any line above.`
		});
	}
	if (absent > 0 && folders.length > 0) {
		const shown = folders.slice(0, FOLDERS_INLINE).map((f) => `${f.folder} (${n(f.tracks)})`);
		lines.push({
			lane: 'absent-folders',
			tone: 'note',
			text: `Most are in ${shown.join(', ')}`,
			title: folders.map((f) => `${n(f.tracks)}: ${f.folder}`).join('\n')
		});
	}
	if (unplugged > 0) {
		lines.push({
			lane: 'absent-volume',
			tone: 'note',
			text: `${n(unplugged)} ${unplugged === 1 ? 'track is' : 'tracks are'} on a drive that is not plugged in`,
			title: null
		});
	}
	return lines;
}

/** What the stems row says, or null when there is nothing to say. Lyrics-style when songs are known. */
export function stemsText(stems: StemsLine): string | null {
	const have = stems.separated !== undefined && stems.not_yet !== undefined;
	const counted = have ? `Stems: ${n(stems.separated ?? 0)} separated, ${n(stems.not_yet ?? 0)} not yet` : null;
	switch (stems.state) {
		case 'ask':
			return counted ? `${counted}. Separate them?` : `${n(stems.pending ?? 0)} tracks have no stems yet. Separate them?`;
		case 'no_source':
			return `${counted ?? `Stems: ${n(stems.pending ?? 0)} tracks have none`}, and this computer cannot make them (${stems.reason})`;
		case 'user_declined':
			return null;
		case 'unknown':
			return `Stems status unknown: ${stems.reason}`;
		case 'done':
			return null;
		default: {
			const exhaustive: never = stems.state;
			throw new Error(`Unhandled stems state: ${exhaustive}`);
		}
	}
}

/** True when the card should offer Retry: some automatic lane is red or cannot run. */
export function offersRetry(summary: EnrichSummary): boolean {
	if (summary.analysis === null) return summary.analysis_error !== null;
	return Object.keys(LANE_LABELS).some((lane) => {
		const c = summary.analysis?.lanes[lane];
		return c !== undefined && (view(c).counts.red || Boolean(c.unavailable));
	});
}

/**
 * True when the card opens expanded: something needs the user's eyes or a
 * click (the stems question, a red lane, a lane that cannot run, an unknown
 * status). Background progress, allowable failures and duds open collapsed.
 */
export function needsAttention(summary: EnrichSummary | null, loadError: string | null, actionError: string | null): boolean {
	if (loadError || actionError || summary === null) return true;
	if (summary.stems.state === 'ask' || summary.stems.state === 'unknown') return true;
	const lines = [...analysisLines(summary), lyricsLine(summary)];
	return lines.some((line) => line !== null && (line.tone === 'failed' || line.tone === 'unavailable'));
}

/** The collapsed card: a lane count, with the lane names on hover. */
export function collapsedLine(summary: EnrichSummary): { text: string; title: string } {
	const running = [...analysisLines(summary), lyricsLine(summary)].filter(
		(line): line is CardLine => line !== null && line.tone === 'working'
	);
	const labels = running.map((line) => {
		if (line.lane === 'lyrics') return 'Lyrics';
		const c = summary.analysis?.lanes[line.lane];
		return c?.usable || c?.usable_songs ? VALUE_LABELS[line.lane] : LANE_LABELS[line.lane];
	});
	if (labels.length === 0) {
		return { text: 'Library: nothing left running', title: 'No analysis lane is still running in the background' };
	}
	const drains = running.map((line) => summary.analysis?.drain?.[line.lane]?.state);
	const allPaused = drains.every((state) => state === 'paused_playing');
	const lanes = `${labels.length} ${labels.length === 1 ? 'lane' : 'lanes'}`;
	return {
		text: allPaused ? `Library: ${lanes} paused while a deck is playing` : `Library: ${lanes} still running`,
		title: `${allPaused ? 'Paused while a deck is playing' : 'Running in the background'}: ${labels.join(', ')}. More shows the counts.`
	};
}
