/**
 * Phrase-ranked mix-out / mix-in windows for AutoPlay Next (issue #11180).
 *
 * Aligns with ``apps/autodj/windows.py`` at the ANLZ layer: phrase starts only,
 * never invented when ``phrases`` is empty. Zehren mix-in salience (0.4 x peak
 * over the next 4 bars) is applied in Python only until per-bar energy is wired
 * here. Callers keep the B-16 / 8-beat loop fallback from ``auto-play-next.ts``
 * when this module offers nothing.
 */

import type { AnlzBeat, AnlzPhrase, AnlzVocals } from '$lib/rb/anlz-types';

export type AutoPlayMixRole = 'out' | 'in';

export interface AutoPlayMixWindow {
	role: AutoPlayMixRole;
	start_bar: number;
	max_bars: number;
	label: string;
	vocal_free_bars: number | null;
	peak_bar: number | null;
	score: number;
	reason: string;
	/** Outro (or tail) has fewer than 4 bars left on the grid for a blend. */
	too_short: boolean;
}

export interface AutoPlayMixTrackInput {
	beats: readonly AnlzBeat[];
	duration_sec: number;
	phrases: readonly AnlzPhrase[];
	phrases_source?: string;
	vocals?: AnlzVocals | null;
}

export interface AutoPlayMixPair {
	phrase_analysis: boolean;
	out: AutoPlayMixWindow | null;
	in: AutoPlayMixWindow | null;
}

const BEATS_PER_BAR = 4;
const MAX_WINDOW_BARS = 64;
const FALLBACK_BARS = 32;
const MIN_BLEND_BARS = 4;
const VOCAL_BAR_THRESHOLD = 0.25;

const OUTRO = 'outro';
const BREAKDOWN = 'breakdown';
const BRIDGE = 'bridge';
const INSTRUMENTAL = 'instrumental';
const INTRO = 'intro';
const BUILD = 'build';
const DROP = 'drop';
const CHORUS = 'chorus';
const VERSE = 'verse';
const UNKNOWN = 'unknown';

const MIX_OUT_LABELS = new Set([OUTRO, BREAKDOWN, BRIDGE, INSTRUMENTAL]);
const MIX_IN_LABELS = new Set([INTRO, BUILD, INSTRUMENTAL]);
const PEAK_LABELS = new Set([DROP, CHORUS]);

const OUT_LABEL_WEIGHT: Record<string, number> = {
	[OUTRO]: 1,
	[BREAKDOWN]: 0.8,
	[BRIDGE]: 0.7,
	[INSTRUMENTAL]: 0.7
};
const IN_LABEL_WEIGHT: Record<string, number> = {
	[INTRO]: 1,
	[BUILD]: 0.8,
	[INSTRUMENTAL]: 0.7
};
const OTHER_LABEL_WEIGHT = 0.3;

const PSSI_HIGH: Record<number, [string, string]> = {
	1: ['Intro', INTRO],
	2: ['Up', BUILD],
	3: ['Down', BREAKDOWN],
	5: ['Chorus', DROP],
	6: ['Outro', OUTRO]
};

const OWN_LABEL: Record<string, string> = {
	buildup: BUILD,
	drop: DROP,
	breakdown: BREAKDOWN,
	cooldown: OUTRO,
	intro: INTRO,
	outro: OUTRO,
	break: BREAKDOWN,
	bridge: BRIDGE,
	inst: INSTRUMENTAL,
	solo: INSTRUMENTAL,
	verse: VERSE,
	chorus: CHORUS
};

export function hasPhraseAnalysis(
	input: Pick<AutoPlayMixTrackInput, 'phrases' | 'phrases_source'>
): boolean {
	if (input.phrases_source === 'none') return false;
	return input.phrases.length > 0;
}

function _medianBpm(beats: readonly AnlzBeat[]): number {
	const bpms = beats.map((beat) => beat.bpm).filter((bpm) => bpm > 0);
	if (bpms.length === 0) return 128;
	bpms.sort((a, b) => a - b);
	return bpms[Math.floor(bpms.length / 2)];
}

function _downbeats(beats: readonly AnlzBeat[]): number[] {
	return beats.filter((beat) => beat.n === 1).map((beat) => beat.t);
}

function _nearestBar(downbeats: readonly number[], seconds: number): number {
	let index = 0;
	while (index < downbeats.length && downbeats[index] < seconds) index += 1;
	if (index >= downbeats.length) return downbeats.length;
	if (index > 0 && seconds - downbeats[index - 1] < downbeats[index] - seconds) return index;
	return index + 1;
}

function _endBar(downbeats: readonly number[], seconds: number): number {
	if (downbeats.length > 0 && seconds > downbeats[downbeats.length - 1])
		return downbeats.length + 1;
	return _nearestBar(downbeats, seconds);
}

function phraseLabel(phrase: AnlzPhrase): [string, string] {
	if (phrase.label) {
		const raw = phrase.label;
		let mapped = OWN_LABEL[raw.trim().toLowerCase()] ?? UNKNOWN;
		if (mapped === UNKNOWN && phrase.model_label) {
			mapped = OWN_LABEL[phrase.model_label.trim().toLowerCase()] ?? UNKNOWN;
		}
		return [raw, mapped];
	}
	const mapped = PSSI_HIGH[phrase.kind];
	if (mapped) return mapped;
	return [`kind ${phrase.kind} mood ${phrase.mood}`, UNKNOWN];
}

interface SectionRow {
	start_bar: number;
	end_bar: number;
	label: string;
}

function _sections(phrases: readonly AnlzPhrase[], downbeats: readonly number[]): SectionRow[] {
	const starts = phrases.map((phrase) => _nearestBar(downbeats, phrase.start_s));
	const rows: SectionRow[] = [];
	const totalBars = downbeats.length;
	for (let index = 0; index < phrases.length; index += 1) {
		const startBar = starts[index];
		let endBar =
			index + 1 < starts.length ? starts[index + 1] : _endBar(downbeats, phrases[index].end_s);
		endBar = Math.min(Math.max(endBar, startBar), totalBars + 1);
		if (endBar - startBar <= 0) continue;
		const [, label] = phraseLabel(phrases[index]);
		rows.push({ start_bar: startBar, end_bar: endBar, label });
	}
	return rows;
}

function _barVocals(
	vocals: AnlzVocals | null | undefined,
	downbeats: readonly number[],
	barS: number
): number[] | null {
	if (!vocals || vocals.status === 'not_analyzed' || downbeats.length === 0) return null;
	const regions = vocals.regions ?? [];
	const coverage: number[] = [];
	for (let barIndex = 0; barIndex < downbeats.length; barIndex += 1) {
		const start = downbeats[barIndex];
		const end = barIndex + 1 < downbeats.length ? downbeats[barIndex + 1] : start + barS;
		let covered = 0;
		for (const region of regions) {
			covered += Math.max(0, Math.min(end, region.end_s) - Math.max(start, region.start_s));
		}
		coverage.push(Math.min(1, covered / Math.max(end - start, 1e-6)));
	}
	return coverage;
}

function _firstVocalBar(barVocals: readonly number[] | null, startBar: number): number | null {
	if (barVocals === null) return null;
	for (let index = Math.max(0, startBar - 1); index < barVocals.length; index += 1) {
		if (barVocals[index] >= VOCAL_BAR_THRESHOLD) return index + 1;
	}
	return null;
}

function _nextPeakBar(sections: readonly SectionRow[], startBar: number): number | null {
	for (const section of sections) {
		if (section.start_bar > startBar && PEAK_LABELS.has(section.label)) return section.start_bar;
	}
	return null;
}

function _vocalFreeRun(
	barVocals: readonly number[] | null,
	startBar: number,
	maxBars: number
): number | null {
	if (barVocals === null) return null;
	const first = _firstVocalBar(barVocals, startBar);
	if (first === null) return maxBars;
	return Math.max(0, Math.min(maxBars, first - startBar));
}

function _tooShortOut(label: string, maxBars: number): boolean {
	return label === OUTRO && maxBars < MIN_BLEND_BARS;
}

function _outWindow(
	input: AutoPlayMixTrackInput,
	sections: readonly SectionRow[],
	downbeats: readonly number[],
	barVocals: readonly number[] | null,
	startBar: number,
	label: string,
	why: string
): AutoPlayMixWindow | null {
	const lastBar = downbeats.length;
	if (lastBar - startBar < 1) return null;
	const maxBars = Math.min(MAX_WINDOW_BARS, lastBar - startBar);
	const vocalFree = _vocalFreeRun(barVocals, startBar, maxBars);
	const labelWeight = OUT_LABEL_WEIGHT[label] ?? OTHER_LABEL_WEIGHT;
	const position = startBar / Math.max(1, lastBar);
	const vocalTerm = vocalFree === null ? 1 : vocalFree / maxBars;
	const lengthTerm = Math.min(1, maxBars / 32);
	const score = 0.45 * labelWeight + 0.2 * vocalTerm + 0.2 * position + 0.15 * lengthTerm;
	const reasons = [why, `${maxBars} bars to the last downbeat`];
	if (vocalFree !== null) reasons.push(`${vocalFree} vocal-free bars`);
	return {
		role: 'out',
		start_bar: startBar,
		max_bars: maxBars,
		label,
		vocal_free_bars: vocalFree,
		peak_bar: null,
		score,
		reason: reasons.join('; '),
		too_short: _tooShortOut(label, maxBars)
	};
}

function _inWindow(
	input: AutoPlayMixTrackInput,
	sections: readonly SectionRow[],
	downbeats: readonly number[],
	barVocals: readonly number[] | null,
	startBar: number,
	label: string,
	why: string
): AutoPlayMixWindow {
	const peak = _nextPeakBar(sections, startBar);
	const firstVocal = _firstVocalBar(barVocals, startBar);
	let limit = downbeats.length - startBar + 1;
	const stops = [peak, firstVocal].filter((bar): bar is number => bar !== null && bar > startBar);
	if (stops.length > 0) limit = Math.min(limit, Math.min(...stops) - startBar);
	const maxBars = Math.max(1, Math.min(MAX_WINDOW_BARS, limit));
	const vocalFree = _vocalFreeRun(barVocals, startBar, maxBars);
	const labelWeight = IN_LABEL_WEIGHT[label] ?? OTHER_LABEL_WEIGHT;
	const position = 1 - (startBar - 1) / Math.max(1, downbeats.length);
	const vocalTerm = vocalFree === null ? 1 : vocalFree / maxBars;
	const lengthTerm = Math.min(1, maxBars / 16);
	const landing = peak !== null ? 1 : 0;
	const score =
		0.4 * labelWeight + 0.15 * vocalTerm + 0.15 * position + 0.15 * lengthTerm + 0.15 * landing;
	const reasons = [why, `${maxBars} usable bars`];
	if (peak !== null) reasons.push(`lands on a peak at bar ${peak}`);
	if (firstVocal !== null && firstVocal >= startBar)
		reasons.push(`first vocal at bar ${firstVocal}`);
	return {
		role: 'in',
		start_bar: startBar,
		max_bars: maxBars,
		label,
		vocal_free_bars: vocalFree,
		peak_bar: peak,
		score,
		reason: reasons.join('; '),
		too_short: false
	};
}

export function mixWindowsForTrack(
	input: AutoPlayMixTrackInput,
	role: AutoPlayMixRole,
	limit = 5
): AutoPlayMixWindow[] {
	if (!hasPhraseAnalysis(input)) return [];
	const downbeats = _downbeats(input.beats);
	if (downbeats.length === 0) return [];
	const sections = _sections(input.phrases, downbeats);
	const barS = (60 / _medianBpm(input.beats)) * BEATS_PER_BAR;
	const barVocals = _barVocals(input.vocals, downbeats, barS);
	const candidates = new Map<number, AutoPlayMixWindow>();

	const offer = (window: AutoPlayMixWindow) => {
		const current = candidates.get(window.start_bar);
		if (current === undefined || window.score > current.score)
			candidates.set(window.start_bar, window);
	};

	if (role === 'out') {
		const tailFrom = Math.max(1, Math.floor(downbeats.length * 0.5));
		for (const section of sections) {
			if (MIX_OUT_LABELS.has(section.label)) {
				const window = _outWindow(
					input,
					sections,
					downbeats,
					barVocals,
					section.start_bar,
					section.label,
					`${section.label} starts here`
				);
				if (window !== null) offer(window);
			} else if (section.start_bar >= tailFrom) {
				const window = _outWindow(
					input,
					sections,
					downbeats,
					barVocals,
					section.start_bar,
					section.label,
					'phrase start in the back half'
				);
				if (window !== null) offer(window);
			}
		}
		if (candidates.size === 0) {
			let fallback = Math.max(1, downbeats.length - FALLBACK_BARS + 1);
			fallback -= (fallback - 1) % 8;
			const window = _outWindow(
				input,
				sections,
				downbeats,
				barVocals,
				fallback,
				UNKNOWN,
				'no mix-out phrase start ranked: last 32 bars'
			);
			if (window !== null) offer(window);
		}
	} else {
		for (const section of sections) {
			if (MIX_IN_LABELS.has(section.label)) {
				offer(
					_inWindow(
						input,
						sections,
						downbeats,
						barVocals,
						section.start_bar,
						section.label,
						`${section.label} starts here`
					)
				);
			}
		}
		const first = sections[0];
		if (first !== undefined && !candidates.has(first.start_bar)) {
			offer(
				_inWindow(
					input,
					sections,
					downbeats,
					barVocals,
					first.start_bar,
					first.label,
					'the first phrase'
				)
			);
		}
		if (candidates.size === 0) {
			offer(
				_inWindow(
					input,
					sections,
					downbeats,
					barVocals,
					1,
					UNKNOWN,
					'no mix-in phrase start ranked: from bar 1'
				)
			);
		}
	}

	return [...candidates.values()]
		.sort((a, b) => b.score - a.score || a.start_bar - b.start_bar)
		.slice(0, Math.max(1, limit));
}

export function pickAutoPlayMixPair(
	outgoing: AutoPlayMixTrackInput,
	incoming: AutoPlayMixTrackInput
): AutoPlayMixPair {
	const phrase = hasPhraseAnalysis(outgoing) && hasPhraseAnalysis(incoming);
	if (!phrase) {
		return { phrase_analysis: false, out: null, in: null };
	}
	const outWindows = mixWindowsForTrack(outgoing, 'out', 1);
	const inWindows = mixWindowsForTrack(incoming, 'in', 1);
	return {
		phrase_analysis: true,
		out: outWindows[0] ?? null,
		in: inWindows[0] ?? null
	};
}

/** Map a 1-based bar index to the beat index of that bar's downbeat, or null. */
export function beatIndexForBar(beats: readonly AnlzBeat[], startBar: number): number | null {
	const downbeatOrdinal = startBar - 1;
	let seen = -1;
	for (let index = 0; index < beats.length; index += 1) {
		if (beats[index].n !== 1) continue;
		seen += 1;
		if (seen === downbeatOrdinal) return index;
	}
	return null;
}
