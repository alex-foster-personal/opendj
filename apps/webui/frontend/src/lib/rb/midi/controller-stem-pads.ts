import type { StemDeckState } from '$lib/rb/stem-types';

/** Factory four-channel order; `other` is the real Demucs harmonics part. */
export const CONTROLLER_NEURAL_STEMS = ['drums', 'bass', 'other', 'vocal'] as const;

export function controllerStemPadsAvailable(stems: StemDeckState): boolean {
	return stems.status === 'ready' && stems.layout === 'demucs4' &&
		CONTROLLER_NEURAL_STEMS.every((stem) => stems.available_controls.includes(stem));
}
