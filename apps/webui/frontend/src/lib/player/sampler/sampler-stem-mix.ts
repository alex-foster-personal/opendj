/**
 * Edge sampler: pure rules for playing a capture's stem settings (EDGESAMP-11872h, spec 3.2, 5.1).
 * Issue #10788 (S11), ported from the v1 reference. The clip pool mixes the stem-set's part clips
 * into one buffer with these gains; the gate is the deck's own (rb/stem-graph.ts stemPartGains): a
 * part sounds when it is not muted and either nothing is soloed or it is soloed, with solo counted
 * only over the parts the stem-set has.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 unity stems play the mix clip and render no parts
 *     [if] every part is 0 dB, unmuted, unsoloed [then] stemsAreUnity is true [else ⛔️]
 *     [if] one part is muted [then] stemsAreUnity is false [else ⛔️]
 *   ✔︎ ✅ 🎯 part gains follow the deck's mute/solo gate
 *     [if] drums are soloed [then] only drums sound [else ⛔️]
 *     [if] a part is both muted and soloed [then] it is silent (mute wins, as on the deck) [else ⛔️]
 *     [if] drums are soloed on a vocals+inst stem-set [then] solo is ignored and both parts sound [else ⛔️]
 *   ✔︎ ✅ 🎯 stems pending (defined once, in sampler-store-logic.ts) is the mirror of playsStems
 *     [if] bass is muted and stems_ready is false [then] stemsPending is true [else ⛔️]
 *   ✔︎ ✅ 🎯 the hot-pool key changes with the stem settings
 *     [if] one gain changes [then] the key changes [else ⛔️]
 */

import { VECTOR_FLOOR_DB } from './sampler-capture-vector';
import type { SamplerSlot, SamplerVector } from './sampler-types';

export type VectorStemPart = 'vocals' | 'drums' | 'bass' | 'inst';
export type VectorStems = NonNullable<SamplerVector['stems']>;
export const VECTOR_STEM_PARTS: readonly VectorStemPart[] = ['vocals', 'drums', 'bass', 'inst'];

/** True when the stem settings leave the mix as is (null = no stems captured). */
export function stemsAreUnity(stems: SamplerVector['stems']): boolean {
	if (stems === null) return true;
	return VECTOR_STEM_PARTS.every((p) => stems[p].gain_db === 0 && !stems[p].mute && !stems[p].solo);
}

/** True when the slot plays a stem mix: non-unity stems on a track whose stem-set exists. */
export function playsStems(slot: Pick<SamplerSlot, 'vector' | 'stems_ready'>): boolean {
	return !stemsAreUnity(slot.vector?.stems ?? null) && slot.stems_ready;
}

/** dB to linear gain; the capture's floor (VECTOR_FLOOR_DB) and below is silence. */
export const dbToLinear = (db: number): number => (db <= VECTOR_FLOOR_DB ? 0 : 10 ** (db / 20));

/** Linear gain per part the stem-set has; parts it lacks are left out. */
export function effectivePartGains(
	stems: VectorStems,
	parts: readonly VectorStemPart[]
): Partial<Record<VectorStemPart, number>> {
	if (parts.length === 0) throw new Error('a stem-set has at least one part');
	const anySolo = parts.some((p) => stems[p].solo);
	const out: Partial<Record<VectorStemPart, number>> = {};
	for (const p of parts) {
		const s = stems[p];
		out[p] = !s.mute && (!anySolo || s.solo) ? dbToLinear(s.gain_db) : 0;
	}
	return out;
}

/** Short stable hash (FNV-1a) of the stem settings, for the hot-pool key. */
export function stemSettingsHash(stems: VectorStems): string {
	const text = VECTOR_STEM_PARTS.map(
		(p) => `${p}:${stems[p].gain_db}:${+stems[p].mute}:${+stems[p].solo}`
	).join('|');
	let h = 0x811c9dc5;
	for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 0x01000193) >>> 0;
	return h.toString(16).padStart(8, '0');
}

/** Hot-pool key: `<slot_id>@<rate>` for the mix clip, `<slot_id>@<rate>#stems-<hash>` for a stem mix. */
export function stemMixPoolKey(
	slotId: string,
	sampleRate: number,
	stems: VectorStems | null
): string {
	return stems === null
		? `${slotId}@${sampleRate}`
		: `${slotId}@${sampleRate}#stems-${stemSettingsHash(stems)}`;
}
