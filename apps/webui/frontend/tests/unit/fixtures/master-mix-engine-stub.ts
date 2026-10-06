/** Stand-in for `$lib/rb/audio-engine.svelte` in master-mix-capture tests:
 *  only the one export the capture module reads, driven by the test. */
type TapPoint = { context: AudioContext; node: GainNode } | null;

let point: TapPoint = null;
export const builds: boolean[] = [];

export function setMasterMixTapPoint(next: TapPoint): void {
	point = next;
}

export function masterMixTapPoint(build: boolean): TapPoint {
	builds.push(build);
	return point;
}
