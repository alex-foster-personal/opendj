/**
 * BeatSyncMax deferred waveform seek (DECKUX-21, issue #3991).
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Downbeat snap defers while playing under BeatSyncMax.
 *     [if] BSM on, playing, unlooped, downbeat snap [then] arm to next downbeat
 *   ✔︎ ✅ 🎯 Shift and Cmd+Shift snaps stay immediate.
 *     [if] snap is exact or beat [then] never arm
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { planHotCueTrigger, type HotCueTriggerPlan } from '$lib/rb/beat-sync-math';
export type WaveformSeekSnap = 'downbeat' | 'beat' | 'exact';

export type WaveformSeekPlan = HotCueTriggerPlan;

export function planWaveformSeek(
	beatSyncMax: boolean,
	playing: boolean,
	loopEngaged: boolean,
	positionSec: number,
	beats: readonly AnlzBeat[],
	snapMode: WaveformSeekSnap
): WaveformSeekPlan {
	if (snapMode !== 'downbeat') return { kind: 'immediate' };
	return planHotCueTrigger(beatSyncMax, playing, loopEngaged, positionSec, beats);
}
