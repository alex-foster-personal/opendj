/**
 * Enrich-on-open card policy (ENRICH-01, ENRICH-02). Pure: the component fetches
 * GET /api/v1/enrich/summary and renders exactly the lines this returns, so
 * every state in specs/state-inventories/library-enrichment.md is unit-testable here.
 *
 * One line per lane, and each state reads differently, because each needs a
 * different action from the user:
 *   done        nothing to say (the line is omitted)
 *   ready       BPM and key: how many tracks have a value from ANY source, and
 *               which (rekordbox, Open DJ, ...). Open DJ's own re-analysis is
 *               a dim second line, so it never reads as missing data
 *   working     "Loudness: 275 of 2,270 done, paused while a deck is playing"
 *   declined    counted apart: an answer, not unfinished work
 *   failed      named reason, and the card offers Retry
 *   unavailable this computer cannot produce the lane, with the reason
 *
 * Every count names its denominator (tracks whose audio is on this Mac), and
 * tracks whose files are not on this Mac get their own line, never a
 * percentage (docs/library-availability.md, the denominator house rule).
 * Every number comes from the summary: nothing is recounted here.
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
};

export type StemsLine = {
	state: 'ask' | 'done' | 'no_source' | 'user_declined' | 'unknown';
	pending: number | null;
	reason: string | null;
};

export type AbsentFolder = { folder: string; tracks: number };

export type EnrichSummary = {
	show: boolean;
	analysis: { lanes: Record<string, LaneCounts>; drain?: Record<string, DrainState> } | null;
	analysis_error: string | null;
	coverage: {
		on_disk: number;
		/** Playability buckets of every library row (library_playable). */
		availability?: Record<string, number>;
		absent_folders?: AbsentFolder[];
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

const presentTitle = (total: number): string => `Counted over the ${n(total)} tracks whose audio is on this computer`;

function reasonsTitle(reasons: Record<string, number> | undefined): string | null {
	const entries = Object.entries(reasons ?? {});
	if (entries.length === 0) return null;
	return entries.map(([why, count]) => `${n(count)}: ${why}`).join('\n');
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

/** BPM or key: ready from any source, then Open DJ's own re-analysis as a dim note. */
function valueLines(lane: string, c: LaneCounts, usable: UsableCounts, drain: DrainState | undefined): CardLine[] {
	const label = VALUE_LABELS[lane];
	const sources = sourcesPhrase(usable.by_source);
	const lines: CardLine[] = [
		{
			lane,
			tone: usable.none > 0 ? 'working' : 'ready',
			text:
				`${label}: ${n(usable.ready)} of ${n(usable.total)} tracks ready` +
				(sources ? ` (${sources})` : '') +
				(usable.none > 0 ? `, ${n(usable.none)} with none yet` : ''),
			title: presentTitle(usable.total)
		}
	];
	if (c.missing > 0 || c.failed > 0) {
		const declined = c.declined ?? 0;
		lines.push({
			lane,
			tone: 'note',
			text:
				`Open DJ ${LANE_PHRASES[lane].replace(/s$/, '')} re-analysis: ${n(c.done)} of ${n(c.total)}, ` +
				(c.missing > 0 ? drainPhrase(drain) : 'finished') +
				(c.failed > 0 ? `, ${n(c.failed)} could not be read` : '') +
				(declined > 0 ? `, ${n(declined)} with no confident answer` : ''),
			title: reasonsTitle(c.failed_reasons) ?? presentTitle(c.total)
		});
	}
	return lines;
}

/** The analysis lane lines, in the drain's order, omitting finished lanes. */
export function analysisLines(summary: EnrichSummary): CardLine[] {
	if (summary.analysis === null) {
		return summary.analysis_error
			? [{ lane: 'analysis', tone: 'unavailable', text: `Analysis status unknown: ${summary.analysis_error}`, title: null }]
			: [];
	}
	const lines: CardLine[] = [];
	for (const [lane, label] of Object.entries(LANE_LABELS)) {
		const c = summary.analysis.lanes[lane];
		if (!c) continue;
		const declined = c.declined ?? 0;
		const drain = summary.analysis.drain?.[lane];
		if (c.usable && VALUE_LABELS[lane]) {
			lines.push(...valueLines(lane, c, c.usable, drain));
			if (c.unavailable) {
				lines.push({ lane, tone: 'unavailable', text: `${label}: this computer cannot re-analyse it (${c.unavailable})`, title: null });
			}
			continue;
		}
		if (c.unavailable) {
			lines.push({ lane, tone: 'unavailable', text: `${label}: this computer cannot produce it (${c.unavailable})`, title: null });
			continue;
		}
		if (c.failed > 0) {
			lines.push({
				lane,
				tone: 'failed',
				text: `${label}: ${n(c.failed)} failed, ${n(c.done)} of ${n(c.total)} done`,
				title: reasonsTitle(c.failed_reasons)
			});
		} else if (c.missing > 0) {
			lines.push({
				lane,
				tone: 'working',
				text: `${label}: ${n(c.done)} of ${n(c.total)} done, ${drainPhrase(drain)}`,
				title: presentTitle(c.total)
			});
		}
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

/** The lyrics line: automatic, so progress only. */
export function lyricsLine(summary: EnrichSummary): CardLine | null {
	const c = summary.coverage;
	if (c === null) {
		return summary.coverage_error
			? { lane: 'lyrics', tone: 'unavailable', text: `Lyrics status unknown: ${summary.coverage_error}`, title: null }
			: null;
	}
	const pending = c.pending.lyrics ?? 0;
	const failed = c.failed.lyrics ?? 0;
	if (pending === 0 && failed === 0) return null;
	const found = c.done.lyrics ?? 0;
	const none = c.terminal.lyrics ?? 0;
	return {
		lane: 'lyrics',
		tone: failed > 0 ? 'failed' : 'working',
		text:
			`Lyrics: ${n(found)} found, ${n(none)} with none available, ${n(pending)} still to look up` +
			(failed > 0 ? `, ${n(failed)} failed` : ''),
		title: null
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

/** What the stems row says, or null when there is nothing to say. */
export function stemsText(stems: StemsLine): string | null {
	switch (stems.state) {
		case 'ask':
			return `${n(stems.pending ?? 0)} tracks have no stems yet. Separate them?`;
		case 'no_source':
			return `Stems: ${n(stems.pending ?? 0)} tracks have none, and this computer cannot make them (${stems.reason})`;
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

/** True when the card should offer Retry: some automatic lane failed or cannot run. */
export function offersRetry(summary: EnrichSummary): boolean {
	if (summary.analysis === null) return summary.analysis_error !== null;
	return Object.keys(LANE_LABELS).some((lane) => {
		const c = summary.analysis?.lanes[lane];
		return c !== undefined && (c.failed > 0 || Boolean(c.unavailable));
	});
}

/**
 * True when the card opens expanded: something needs the user's eyes or a
 * click (the stems question, a failure, a lane that cannot run, an unknown
 * status). Background progress alone opens collapsed, so the card does not
 * sit over the track table's right-hand columns while nothing is asked.
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
		return summary.analysis?.lanes[line.lane]?.usable ? VALUE_LABELS[line.lane] : LANE_LABELS[line.lane];
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
