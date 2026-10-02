/**
 * Enrich-on-open card policy (ENRICH-01). Pure: the component fetches
 * GET /api/v1/enrich/summary and renders exactly the lines this returns, so
 * every state in docs/library-enrichment-states.md is unit-testable here.
 *
 * One line per lane, and each state reads differently, because each needs a
 * different action from the user:
 *   done        nothing to say (the line is omitted)
 *   working     "Key: 44 of 1,274 done, the rest running in the background"
 *   declined    counted apart: an answer, not unfinished work
 *   failed      named reason, and the card offers Retry
 *   unavailable this computer cannot produce the lane, with the reason
 */

export type LaneCounts = {
	total: number;
	done: number;
	missing: number;
	failed: number;
	declined?: number;
	failed_reasons?: Record<string, number>;
	declined_reasons?: Record<string, number>;
	unavailable?: string | null;
};

export type StemsLine = {
	state: 'ask' | 'done' | 'no_source' | 'user_declined' | 'unknown';
	pending: number | null;
	reason: string | null;
};

export type EnrichSummary = {
	show: boolean;
	analysis: { lanes: Record<string, LaneCounts> } | null;
	analysis_error: string | null;
	coverage: {
		on_disk: number;
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
	tone: 'working' | 'failed' | 'unavailable' | 'note';
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

const n = (value: number): string => value.toLocaleString('en-US');

function reasonsTitle(reasons: Record<string, number> | undefined): string | null {
	const entries = Object.entries(reasons ?? {});
	if (entries.length === 0) return null;
	return entries.map(([why, count]) => `${n(count)}: ${why}`).join('\n');
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
				text: `${label}: ${n(c.done)} of ${n(c.total)} done, the rest running in the background`,
				title: `Counted over the ${n(c.total)} tracks whose audio is on this computer`
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
	return analysisLines(summary).some((line) => line.tone === 'failed' || line.tone === 'unavailable');
}
