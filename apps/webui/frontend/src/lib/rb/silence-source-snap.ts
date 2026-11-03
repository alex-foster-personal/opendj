/**
 * Deck + mixer state -> the source gate's view of one deck (AUDIOLIVE-09,
 * AUDIOLIVE-12, AUDIOLIVE-17). Pure, so the "was sound expected here?"
 * question is testable against the same mixer and stem laws the graph applies
 * (`onAirGain`, `stemPartGains`) without an AudioContext.
 */

import { onAirGain } from '$lib/rb/master-election';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { STEM_LAYOUT_PARTS, stemPartGains, type StemPart } from '$lib/rb/stem-graph';
import type { StemDeckState } from '$lib/rb/stem-types';
import type { PeakEnvelope, SilenceSourceDeckSnap } from '$lib/rb/silence-source-pcm';

export interface SilenceSourceDeckInput {
	/** The deck this snapshot is for: its auto gain is per deck (AUTOGAIN-02). */
	id: DeckId;
	deck: { playing: boolean; audible: boolean; stems: StemDeckState };
	channel: { fader: number; trim: number; assign: CrossfaderAssign };
	crossfader: number;
	master: number;
	buffer: AudioBuffer | null;
	position_sec: number;
	/**
	 * The live processor's part envelopes when the deck's signal path is a
	 * stem graph, null when it is the mix. Read from the processor rather than
	 * inferred from `stems.status`, because the processor is what is playing.
	 */
	stem_envelopes: Partial<Record<StemPart, PeakEnvelope>> | null;
}

/**
 * EQ is left out because a full cut is a dB threshold, not a zero, so an
 * EQ-killed deck still reads as open. A stem graph whose deck state has no
 * ready layout yields no parts, which the gate refuses to read as silence.
 */
export function silenceSourceDeckSnap(input: SilenceSourceDeckInput): SilenceSourceDeckSnap {
	const { deck, channel } = input;
	const mixerGain = onAirGain(
		{
			id: input.id,
			loaded: true,
			playing: true,
			beat_sync_enabled: false,
			fader: channel.fader,
			trim: channel.trim,
			assign: channel.assign
		},
		input.crossfader,
		input.master
	);
	const snap: SilenceSourceDeckSnap = {
		claims_live: deck.playing || deck.audible,
		buffer: input.buffer,
		position_sec: input.position_sec,
		master_path_gain: mixerGain
	};
	if (input.stem_envelopes === null) return snap;
	const stems = deck.stems;
	if (stems.status !== 'ready' || stems.layout === null) return { ...snap, stem_parts: [] };
	const envelopes = input.stem_envelopes;
	const gains = stemPartGains(stems.controls, stems.layout);
	return {
		...snap,
		stem_parts: STEM_LAYOUT_PARTS[stems.layout].map((part) => ({
			gain: gains[part] ?? Number.NaN,
			envelope: envelopes[part] ?? null
		}))
	};
}
