// A throwaway beat FX effect in the exact shape of a new file under rb/fx/beat-fx-effects/ (issue #13726).
import { RecordingKernelHost } from '$lib/rb/fx/beat-fx-effects/kernel-host';
import { registerBeatFxEffect } from '$lib/rb/fx/beat-fx-effects/registry';

registerBeatFxEffect({
	name: 'throwaway',
	levelLaw: 'balance',
	builtOnMain: true,
	usesDivision: true,
	assertDivisionAllowed: () => undefined,
	createHost: () => new RecordingKernelHost('throwaway')
});
