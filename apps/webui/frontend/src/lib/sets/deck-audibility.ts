/**
 * Did the room actually hear this deck?
 *
 * Split out of `deck-observer-emitter.ts`, which had grown past the 600-line
 * review threshold. The seam is real rather than arithmetic: everything here
 * is a pure question about the engine's signal path, with no timers, no
 * buffering and no HTTP, and the emitter is the opposite.
 */

import { TRIM_MAX_GAIN } from '$lib/player/constants';
import { isMasterMuted } from '$lib/player/master-mute.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import { stemPartGains } from '$lib/rb/stem-graph';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import type { PerformanceState } from '$lib/rb/performance-ipc.svelte';

/** Linear gain below which the master bus is silent, -80 dBFS.
 *
 *  Not an exact-zero test, because exact zero is unreachable through the
 *  engine's equal-power crossfade law: a fully parked crossfader evaluates
 *  `Math.cos(Math.PI / 2)`, which is 6.1e-17 rather than 0. -80 dBFS sits far
 *  below any playback noise floor, so nothing this rejects was heard, and it
 *  is a floor rather than a taste threshold: it never has to decide whether a
 *  quiet deck counts as played. */
export const SILENCE_GAIN_EPSILON = 1e-4;

// ------------------------------------------------------------- audibility ---

/**
 * WHY `deck.audible` IS NOT ON ITS OWN THE ANSWER (Codex #650 P1, confirmed).
 *
 * AGENTS.md pins `audible` to the output presentation clock plus the
 * acknowledged schedule timeline, and `observePresentedTransportTimeline`
 * honors that literally: it computes `selected.active && !deckReachedEnd(...)`
 * and never reads the mixer. So `deck.audible` means "the output is presenting
 * this deck's schedule", NOT "the room heard it".
 *
 * The mixer is a separate gain stage downstream. Pre-cueing a track in the
 * headphones with the channel fader down, or with the crossfader parked on the
 * other bus, is the single most ordinary thing a DJ does, and it leaves
 * `deck.audible` true. Forwarding that flag banked dwell and wrote a
 * `track_loaded` row for tracks the audience never heard, which is precisely
 * what `apps/sets/sources/opendj_source.py` says it must not do: "a deck can be
 * in the playing transport state while every stem is muted or its channel fader
 * is down, and a track nobody heard is not a track that was played".
 *
 * So the wire flag is transport-audible AND routed to the master bus at a gain
 * above silence.
 *
 * WHAT THIS GATES ON, and why each one is exact rather than a threshold.
 * Every stage below is either a multiplicative gain that can be exactly zero,
 * or a mute flag. None of them needs a judgment call about how quiet is too
 * quiet, which matters because a wrong threshold DELETES a real play with no
 * record that anything was removed:
 *
 *   trim, channel fader, crossfader, master  -- the mixer chain
 *   the final master-mute node               -- `?muted=1` and the TopBar mute
 *   every stem part gained to zero           -- nothing left to hear
 *
 * The master mute is a separate GainNode sitting last before
 * `AudioContext.destination` (`player/master-mute.svelte.ts`), so it is NOT
 * `mixer.master` and `PerformanceState` does not carry it. It is read through
 * that module's own programmatic accessor, which is the same surface the test
 * agents use. It also sits downstream of the external-merger branch, so it
 * gates a `?extroute=` deck too, unlike the crossfader and master gain.
 *
 * Stems gate through the engine's own `stemPartGains`, so mute, solo and layout
 * ownership are all answered by the function that drives the real gain nodes
 * rather than by a copy that can drift from it.
 *
 * STILL NOT GATED: EQ. Three knobs fully cut also silences a channel, but the
 * EQ is dB shelving rather than a linear multiplier, so "cut enough to be
 * silent" is a genuine threshold and this refuses to invent one. Consequence
 * while it stays open, recorded in `.planning/TECH-DEBT.md`: a deck killed
 * purely on EQ still accrues `audible_s`.
 */

/** Equal-power crossfade gain for one bus assignment at position x (0..1).
 *
 *  Mirrors `_xfGainFor` in `audio-engine.svelte.ts`, which is module-private.
 *  Copying three lines is the smaller cost: the alternative was exporting a
 *  symbol from a file with several branches open on it, and this module's
 *  whole point is that it edits nothing over there. The test named "the crossfade
 *  law here still matches the one the engine applies" pins the two together
 *  through `engineBlockAfter`, so a change over there re-points this loudly. */
function _crossfaderGain(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

/**
 * Decks the `?extroute=` experiment wires straight to a USB output pair.
 *
 * Those decks leave the graph at the channel fader and reach an external
 * mixer, bypassing the crossfader and master gain entirely
 * (`audio-engine.svelte.ts`, the `extsplit` branch), so gating them on a
 * parked crossfader would delete a play the room did hear.
 *
 * The set is also read as a MODE, not only as a membership test, because
 * external routing REPLACES the internal master rather than running beside it.
 * See `masterPathGain`.
 *
 * Malformed segments are skipped rather than raised on: the engine parses the
 * same parameter at graph build and throws there, so a page running at all has
 * already passed that validation, and a second disagreeing validator here
 * could only ever be wrong in a new way.
 */
export function externallyRoutedDecks(search: string): ReadonlySet<DeckId> {
	const routed = new Set<DeckId>();
	const raw = new URLSearchParams(search).get('extroute');
	if (raw === null) return routed;
	for (const part of raw.split(',')) {
		const deck = Number(part.split(':')[0]);
		if (deck === 1 || deck === 2 || deck === 3 || deck === 4) routed.add(deck);
	}
	return routed;
}

/**
 * Linear gain from this deck's channel to the speakers, per the engine graph.
 *
 * The chain is `trim -> EQ -> fader -> crossfader -> master`
 * (`audio-engine.svelte.ts`). EQ is excluded on purpose, see above.
 *
 * `?extroute=` splits the graph in two, and the second half is easy to get
 * backwards. Read `_ensureGraph`: master reaches `_masterMuteGain` ONLY in the
 * `routing === null` branch, through `wirePracticeBlendIntoMasterPath` (master
 * at unity in practice mode, `player/headphones.ts` `practiceMainGains`) and
 * `wireSplitCableIntoMasterPath`. When any deck is externally routed,
 * the destination is fed by `_externalMerger` alone, and `_masterGain`'s only
 * remaining consumer is `ensureHeadphoneGraph`'s monitor tap. So in that mode
 * an UNMAPPED deck plays into the DJ's headphones and reaches no speaker, which
 * is the opposite of the mapped case, and it returns 0 here.
 *
 * A partial map such as `?extroute=1:1` is therefore the interesting one: deck
 * 1 reaches the room through USB, and decks 2 to 4 reach nothing.
 */
export function masterPathGain(
	state: PerformanceState,
	deckId: DeckId,
	routedDecks: ReadonlySet<DeckId> = new Set()
): number {
	const channel = state.mixer.channels[deckId];
	const chain = channel.trim * TRIM_MAX_GAIN * channel.fader;
	// A routed deck leaves at the fader; crossfader and master are not its path.
	if (routedDecks.has(deckId)) return chain;
	// Unmapped, but external routing is live: headphone monitor only.
	if (routedDecks.size > 0) return 0;
	return chain * _crossfaderGain(channel.assign, state.mixer.crossfader) * state.mixer.master;
}

/** Is every part of this deck's stem bundle gained to zero?
 *
 *  Calls the PRODUCTION law, `stemPartGains` in `rb/stem-graph.ts`, which is
 *  the same function `AlignedStemDeckProcessor.setControls` writes to the gain
 *  nodes. Reusing it rather than mirroring it is what makes this exact: it
 *  already returns a per-part gain with no threshold, and it already knows the
 *  two rules a re-implementation here would have had to rediscover.
 *
 *  Mute alone is NOT the test, which is where the first attempt at this was
 *  wrong. Solo silences every part that is not soloed, so a deck with VOCAL
 *  soloed AND muted plays nothing while two of its three controls are unmuted.
 *  Layout ownership is the second rule: solo is evaluated only over the
 *  controls the layout owns, so a roformer2 deck's inert DRUMS flag cannot
 *  silence the two parts that are real.
 *
 *  False unless the bundle is `ready`: an unavailable, loading or errored
 *  bundle is not in the signal path at all, so its control flags say nothing
 *  about what came out of the speakers. */
export function everyStemPartSilent(
	deck: Pick<PerformanceState['decks'][DeckId], 'stems'>
): boolean {
	const stems = deck.stems;
	if (stems.status !== 'ready' || stems.layout === null) return false;
	const gains = Object.values(stemPartGains(stems.controls, stems.layout));
	return gains.length > 0 && gains.every((gain) => gain === 0);
}

/**
 * Did the room hear this deck?
 *
 * `masterMuted` is injectable so a test can drive it without a real
 * AudioContext; production reads the live master-mute node.
 */
export function deckWasHeard(
	state: PerformanceState,
	deckId: DeckId,
	routedDecks: ReadonlySet<DeckId> = new Set(),
	masterMuted: boolean = isMasterMuted()
): boolean {
	const deck = state.decks[deckId];
	if (!deck.audible) return false;
	// Last node before the destination, so it silences routed decks too.
	if (masterMuted) return false;
	if (everyStemPartSilent(deck)) return false;
	return masterPathGain(state, deckId, routedDecks) >= SILENCE_GAIN_EPSILON;
}
