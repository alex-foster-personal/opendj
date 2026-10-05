/**
 * RESCUE-07 reactive holder: the "Resume N decks" offer shown after a reload
 * stopped playing decks. Pure decisions live in reload-resume.ts.
 */
import type { DeckId } from '$lib/rb/deck-id';
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import type { PerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { reloadResumeOfferMessage, type ReloadResumeOffer } from '$lib/rb/reload-resume';

export const reloadResume = $state<{ offer: ReloadResumeOffer | null; error: string | null }>({
	offer: null,
	error: null
});

export function offerReloadResume(offer: ReloadResumeOffer | null, autoPlayOn: boolean): void {
	reloadResume.offer = offer;
	reloadResume.error = null;
	if (offer === null) return;
	const message = reloadResumeOfferMessage(offer, autoPlayOn);
	recordPerfEvent('reload-resume-offered', message, null, 'warn');
	console.warn(message);
}

export function dismissReloadResume(): void {
	reloadResume.offer = null;
	reloadResume.error = null;
}

/**
 * The banner's Resume button. Plays each offered deck in order through the
 * ordinary `play` command, so the first one takes master by play-claim and
 * Beat Sync followers lock to it, exactly as `opendj play <deck>` would.
 * A refusal stays on screen with its reason rather than vanishing.
 */
export async function acceptReloadResume(
	dispatch: (command: PerformanceCommand) => Promise<unknown>
): Promise<DeckId[]> {
	const offer = reloadResume.offer;
	if (offer === null) return [];
	const started: DeckId[] = [];
	try {
		for (const deck of offer.decks) {
			await dispatch({ type: 'play', deck, playing: true });
			started.push(deck);
		}
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		reloadResume.error = `Resume stopped at deck ${offer.decks[started.length]}: ${message}`;
		recordPerfEvent('reload-resume-failed', `[reload-resume] ${reloadResume.error}`, null, 'error');
		console.error(`[reload-resume] ${reloadResume.error}`);
		return started;
	}
	recordPerfEvent(
		'reload-resume-accepted',
		`[reload-resume] resumed deck(s) ${started.join(',')}; deck ${started[0]} plays first and takes master`,
		null,
		'info'
	);
	reloadResume.offer = null;
	return started;
}
