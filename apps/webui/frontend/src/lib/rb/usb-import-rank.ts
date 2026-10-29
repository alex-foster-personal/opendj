/**
 * Rank a USB stick's tracks by what fits the set while a DJ may be performing.
 *
 * A folder import lands tags only. BPM, key and beatgrid come afterwards, and
 * a stick can hold thousands of files. When a deck is playing, tracks that
 * fit that tempo and key rank first and fill the likely-next bar. The ranking
 * is a view: the analysis drain itself runs in library order. Everything else stays in score order for a windowed list, so the
 * performance view never mounts the whole stick.
 */
import { camelotKeysAreCompatible } from '$lib/player/key/camelot';
import { classifyBpmCompatibility } from '$lib/rb/bpm-heat';

export const USB_IMPORT_NEXT_CAP = 5;
/** How many missing grids to treat as "analyze now" while a deck is playing. */
export const USB_IMPORT_SOON_CAP = 24;

export interface UsbImportTrack {
	id: string;
	title: string;
	artist: string | null;
	bpm: number | null;
	key: string | null;
	play_count: number;
	has_analysis: boolean;
	file_present: boolean;
}

export interface UsbImportContext {
	/** A deck is currently outputting. Analysis stays behind that. */
	playing: boolean;
	bpm: number | null;
	key: string | null;
}

export type UsbImportPhase = 'next' | 'soon' | 'ready' | 'later' | 'skip';

export interface RankedUsbImport extends UsbImportTrack {
	phase: UsbImportPhase;
	reason: string;
	score: number;
}

export interface UsbImportPhaseCounts {
	next: number;
	soon: number;
	ready: number;
	later: number;
	skip: number;
}

export interface UsbImportPlan {
	performing: boolean;
	headline: string;
	next: RankedUsbImport[];
	queue: RankedUsbImport[];
	counts: UsbImportPhaseCounts;
}

const EMPTY_COUNTS: UsbImportPhaseCounts = {
	next: 0,
	soon: 0,
	ready: 0,
	later: 0,
	skip: 0
};

/** parseUsbLibraryWire already refuses a bad count; one reaching here is a bug. */
function playsOf(track: UsbImportTrack): number {
	if (!Number.isFinite(track.play_count) || track.play_count < 0) {
		throw new Error(`USB track ${track.id} has play count ${track.play_count}, not a non-negative number`);
	}
	return track.play_count;
}

/** Play count breaks ties inside a band. It must not outrank a better band,
 * so each band sits a billion apart. */
const SCORE_STEP = 1_000_000_000;

function bandScore(band: number, plays: number): number {
	const safe = Math.min(plays, SCORE_STEP - 1);
	return band * SCORE_STEP + safe;
}

function keyText(value: string | null): string | null {
	if (value === null) return null;
	const trimmed = value.trim();
	return trimmed === '' ? null : trimmed;
}

function tempoWords(fold: number): string {
	if (fold === 2) return 'double-time';
	if (fold === 0.5) return 'half-time';
	return 'same tempo';
}

function countsOf(queue: readonly RankedUsbImport[]): UsbImportPhaseCounts {
	const counts = { ...EMPTY_COUNTS };
	for (const row of queue) counts[row.phase] += 1;
	return counts;
}

function headlineFor(
	ctx: UsbImportContext,
	aimed: boolean,
	queue: readonly RankedUsbImport[]
): string {
	if (queue.length === 0) return 'This stick has no tracks to analyze.';
	if (queue.every((row) => row.phase === 'skip')) {
		return 'Every file on this stick is missing, so nothing can be analyzed.';
	}
	if (ctx.playing && aimed) {
		return 'A deck is playing. Tracks that fit its BPM and key are listed first. Analysis runs in the background, in library order.';
	}
	if (ctx.playing) {
		return 'A deck is playing, with no BPM or key to aim at. Your most-played tracks are listed first. Analysis runs in the background, in library order.';
	}
	if (aimed) {
		return 'Nothing is playing. These fit the track that is loaded, so they are listed first.';
	}
	return 'Nothing is loaded. The tracks you play most are listed first.';
}

function scoreTrack(track: UsbImportTrack, ctx: UsbImportContext, aimed: boolean): RankedUsbImport {
	const plays = playsOf(track);
	if (!track.file_present) {
		return {
			...track,
			play_count: plays,
			phase: 'skip',
			reason: 'file missing on the stick',
			score: -1
		};
	}

	const bpmFit = classifyBpmCompatibility(track.bpm, ctx.bpm);
	const trackKey = keyText(track.key);
	const deckKey = keyText(ctx.key);
	const tempoKnown = bpmFit !== null;
	const keyKnown = deckKey !== null && trackKey !== null;
	const tempoOk = bpmFit !== null && bpmFit.compatible;
	const keyOk = keyKnown && camelotKeysAreCompatible(trackKey, deckKey);
	const tempoBad = tempoKnown && !tempoOk;
	const keyBad = keyKnown && !keyOk;
	const fits = (tempoOk || keyOk) && !tempoBad && !keyBad;

	if (fits) {
		const parts: string[] = [];
		if (tempoOk && bpmFit !== null) parts.push(tempoWords(bpmFit.fold));
		if (keyOk && trackKey !== null && deckKey !== null) parts.push(`${trackKey} fits ${deckKey}`);
		const why = parts.join(', ');
		if (track.has_analysis) {
			return {
				...track,
				play_count: plays,
				phase: 'ready',
				reason: `${why}. Export lists a grid`,
				score: bandScore(4, plays)
			};
		}
		return {
			...track,
			play_count: plays,
			phase: 'soon',
			reason: `${why}. Needs a grid`,
			score: bandScore(3, plays)
		};
	}

	if (!aimed && !track.has_analysis) {
		return {
			...track,
			play_count: plays,
			phase: 'soon',
			reason: ctx.playing
				? 'No BPM or key on the playing deck yet. Most-played tracks first'
				: 'No grid yet. Most-played tracks first',
			score: bandScore(3, plays)
		};
	}

	if (track.has_analysis) {
		return {
			...track,
			play_count: plays,
			phase: 'later',
			reason: aimed ? 'Grid ready, tempo or key is off this set' : 'Grid already on the stick',
			score: bandScore(1, plays)
		};
	}
	return {
		...track,
		play_count: plays,
		phase: 'later',
		reason: aimed
			? 'Analyze after the tracks that fit what is playing'
			: 'Analyze after the tracks you play most',
		score: bandScore(1, plays)
	};
}

function byRank(a: RankedUsbImport, b: RankedUsbImport): number {
	if (b.score !== a.score) return b.score - a.score;
	return a.title.localeCompare(b.title);
}

function capSoonWhilePlaying(ranked: RankedUsbImport[]): void {
	let seen = 0;
	for (const row of ranked) {
		if (row.phase !== 'soon') continue;
		seen += 1;
		if (seen <= USB_IMPORT_SOON_CAP) continue;
		row.phase = 'later';
		row.reason = 'Queued behind the tracks that fit this set';
		row.score = bandScore(2, row.play_count);
	}
}

function pickNext(ranked: readonly RankedUsbImport[]): RankedUsbImport[] {
	const ready = ranked.filter((row) => row.phase === 'ready');
	const soon = ranked.filter((row) => row.phase === 'soon');
	const pool = ready.length + soon.length > 0 ? [...ready, ...soon] : ranked.filter((row) => row.phase === 'later');
	return pool.slice(0, USB_IMPORT_NEXT_CAP).map((row) => ({
		id: row.id,
		title: row.title,
		artist: row.artist,
		bpm: row.bpm,
		key: row.key,
		play_count: row.play_count,
		has_analysis: row.has_analysis,
		file_present: row.file_present,
		phase: 'next',
		score: row.score,
		reason: row.phase === 'later' ? 'Most played on this stick' : row.reason
	}));
}

/** Rank `tracks` for the import analysis list and the likely-next bar. */
export function rankUsbImport(
	tracks: readonly UsbImportTrack[],
	ctx: UsbImportContext
): UsbImportPlan {
	const aimed = ctx.bpm !== null || keyText(ctx.key) !== null;
	const ranked = tracks.map((track) => scoreTrack(track, ctx, aimed));
	ranked.sort(byRank);
	if (ctx.playing) capSoonWhilePlaying(ranked);
	ranked.sort(byRank);
	const next = pickNext(ranked);
	const picked = new Map(next.map((row) => [row.id, row]));
	const queue = ranked.map((row) => picked.get(row.id) ?? row);
	return {
		performing: ctx.playing,
		headline: headlineFor(ctx, aimed, queue),
		next,
		queue,
		counts: countsOf(queue)
	};
}
