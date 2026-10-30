/**
 * Transition try-er panel logic (TRYER-14), pure so node tests drive it.
 *
 * The documents are the ones `POST /api/v1/dj/try` returns (TRYER-10): this
 * module never invents a score, a flag or a candidate. A candidate the scorer
 * could not score (`score: null`) is shown as unscored, never as zero, and is
 * never "best".
 */

import type { components } from '$lib/api-types';

export const TRYER_FLAG_ID = 'dj.tryer';
/** One letter per candidate the route can return (n is 1..12); keys 1-6 reach the first six. */
export const CHIP_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L'] as const;
export const NOT_IMPLEMENTED_TITLE = 'not implemented - see PARITY-TODO';

export const RANK_MEASURED = 'measured';

/** The generated OpenAPI shapes (TRYER-10/15): the server's TryJobOut, never a hand copy. */
export type TryCandidate = components['schemas']['CandidateOut'];
export type TryJob = components['schemas']['TryJobOut'];

export type TryerAction =
	| { kind: 'play'; slot: number }
	| { kind: 'toggle' }
	| { kind: 'swap' }
	| { kind: 'pick' }
	| { kind: 'blind' }
	| { kind: 'cancel' };

export function tryerEnabledIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): boolean {
	return flags.find((flag) => flag.flag_id === TRYER_FLAG_ID)?.enabled === true;
}

/** The key map of the research UX (file 10 section 6) that this build ships. */
export function keyAction(key: string): TryerAction | null {
	if (/^[1-6]$/.test(key)) return { kind: 'play', slot: Number(key) - 1 };
	if (key === ' ') return { kind: 'toggle' };
	if (key === 'x' || key === 'X') return { kind: 'swap' };
	if (key === 'Enter') return { kind: 'pick' };
	if (key === 'b' || key === 'B') return { kind: 'blind' };
	if (key === 'Escape') return { kind: 'cancel' };
	return null;
}

/** Whether the panel's window-level shortcuts must yield, because focus sits on a control that has its own
 *  keyboard activation (Enter on a focused Pick button must pick THAT candidate, not the auditioned one). */
export function shortcutsYieldTo(target: { tagName: string; isContentEditable: boolean } | null): boolean {
	if (target === null) return false;
	return target.isContentEditable || ['BUTTON', 'A', 'INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
}

/** Play attempts by generation: a newer audition or a stop supersedes a pending one, whose rejection is then
 *  ignored rather than reported as a failure of the candidate now playing. */
export function playbackAttempts(): { begin: () => number; supersede: () => void; isCurrent: (n: number) => boolean } {
	let generation = 0;
	return {
		begin: () => ++generation,
		supersede: () => {
			generation += 1;
		},
		isCurrent: (n: number) => n === generation
	};
}

/** The part of HTMLAudioElement an auditioner drives (a fake in tests). */
export type AudioLike = {
	currentTime: number;
	play: () => Promise<void>;
	pause: () => void;
	addEventListener: (type: 'ended' | 'error', listener: () => void) => void;
};

export type AuditionEvents = {
	onEnded: () => void;
	onFailure: (candidateId: string, reason: string) => void;
};

/**
 * Plays one candidate at a time, each source on its OWN media element, so an event is bound to the element (and
 * the source) that raised it: an error queued by an earlier source, or after a newer audition or a stop, finds
 * its element is no longer current and changes nothing. Switching keeps the playhead position.
 */
export function createAuditioner(makeAudio: (url: string) => AudioLike, events: AuditionEvents) {
	const attempts = playbackAttempts();
	let current: { element: AudioLike; id: string; url: string; attempt: number } | null = null;
	const isLive = (element: AudioLike) =>
		current !== null && current.element === element && attempts.isCurrent(current.attempt);
	return {
		play(candidateId: string, url: string): void {
			// A candidate id repeats across jobs (c1, c2...), so a source is its id AND its URL.
			if (current === null || current.id !== candidateId || current.url !== url) {
				const position = current?.element.currentTime ?? 0;
				current?.element.pause();
				const element = makeAudio(url);
				element.currentTime = position;
				element.addEventListener('ended', () => {
					if (isLive(element)) events.onEnded();
				});
				element.addEventListener('error', () => {
					if (isLive(element)) events.onFailure(candidateId, 'the render did not load (render the pair again)');
				});
				current = { element, id: candidateId, url, attempt: 0 };
			}
			const live = current;
			live.attempt = attempts.begin();
			const attempt = live.attempt;
			live.element.play().catch((failure: unknown) => {
				if (!attempts.isCurrent(attempt) || current !== live) return;
				events.onFailure(candidateId, failure instanceof Error ? failure.message : String(failure));
			});
		},
		stop(): void {
			attempts.supersede();
			current?.element.pause();
		},
		/** Seconds into ``candidateId``'s render while it is the current source, else null (TRYER-16 playhead). */
		position(candidateId: string): number | null {
			return current !== null && current.id === candidateId ? current.element.currentTime : null;
		},
		/** Play ``candidateId`` from ``seconds`` (a click on its lanes). */
		playFrom(candidateId: string, url: string, seconds: number): void {
			this.play(candidateId, url);
			if (current !== null) current.element.currentTime = seconds;
		},
		/** Forget the current element and its position: a new job never resumes the previous job's audio. */
		reset(): void {
			attempts.supersede();
			current?.element.pause();
			current = null;
		}
	};
}

function words(name: string): string {
	return name.replaceAll('_', ' ');
}

/** Red flags in words: every veto that fired, with its measured value and limit. */
export function redFlags(candidate: TryCandidate): string[] {
	return candidate.vetoes.map(
		(veto) => `${words(veto.name)} ${veto.value} (limit ${veto.threshold})`
	);
}

/** Factors the scorer could not measure, with its reason, so a gap never reads as a pass. */
export function unmeasured(candidate: TryCandidate): string[] {
	return Object.entries(candidate.unknown).map(
		([factor, reason]) => `${words(factor)}: ${reason}`
	);
}

/** Notes (red flags and unmeasured gaps) every candidate carries, in the first candidate's order: shown once above
 * the cards so each card lists only what sets it apart. Fewer than two candidates share nothing. */
export function sharedNotes(candidates: readonly TryCandidate[]): string[] {
	if (candidates.length < 2) return [];
	const notesOf = (c: TryCandidate) => [...redFlags(c), ...unmeasured(c)];
	const rest = candidates.slice(1).map((c) => new Set(notesOf(c)));
	return notesOf(candidates[0]).filter((note) => rest.every((notes) => notes.has(note)));
}

export function isVetoed(candidate: TryCandidate): boolean {
	return candidate.vetoes.length > 0;
}

/** What the "Use best" button picks (TRYER-15): exactly the server's `job.best`, the execution layer's
 *  `best_candidate`. The panel never recomputes a best of its own, so it can never disagree with the server.
 *  Null when the server named none, or a pick is already stored (undo first). */
export function useBestTarget(job: TryJob): string | null {
	if (job.pick !== null) return null;
	return job.best;
}

/** Index of the first chip whose rank group differs from the one before it (the divider), or null. */
export function groupDividerIndex(chips: readonly TryCandidate[]): number | null {
	const index = chips.findIndex((chip, i) => i > 0 && chip.rank_group !== chips[i - 1].rank_group);
	return index === -1 ? null : index;
}

/** Blind mode: a shuffled display order (Fisher-Yates over an injected random source). */
export function shuffledOrder(
	ids: readonly string[],
	random: () => number
): string[] {
	const order = [...ids];
	for (let i = order.length - 1; i > 0; i -= 1) {
		const j = Math.floor(random() * (i + 1));
		[order[i], order[j]] = [order[j], order[i]];
	}
	return order;
}

/** Chips in display order (ranked, or shuffled when blind), lettered A-F by position. */
export function chipOrder(
	job: TryJob,
	blindOrder: readonly string[] | null
): TryCandidate[] {
	const byId = new Map(
		job.candidates.map((candidate) => [candidate.id, candidate])
	);
	const ids = blindOrder ?? job.candidates.map((candidate) => candidate.id);
	if (ids.length > CHIP_LETTERS.length)
		throw new Error(`${ids.length} candidates, but chips are lettered only up to ${CHIP_LETTERS.length}`);
	return ids.map((id) => {
		const candidate = byId.get(id);
		if (candidate === undefined)
			throw new Error(
				`blind order names candidate ${id} the job does not hold`
			);
		return candidate;
	});
}

/** Score bar width in percent; null when unscored (the bar is not drawn); an out-of-range score throws. */
export function scoreBarPercent(candidate: TryCandidate): number | null {
	if (candidate.score === null) return null;
	// M is 0..1 by contract (the route validates it); anything else is a scorer defect, shown as one, never clamped.
	if (!(candidate.score >= 0 && candidate.score <= 1))
		throw new Error(`candidate ${candidate.id} score ${candidate.score} is outside 0..1`);
	return Math.round(candidate.score * 100);
}
