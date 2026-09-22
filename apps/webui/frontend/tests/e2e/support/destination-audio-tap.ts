import type { Page } from '@playwright/test';

/** Destination RMS floor. Silence (gain 0 anywhere downstream) reads exactly 0. */
export const DESTINATION_RMS_FLOOR = 1e-3;

type DestTapWindow = Window & { __destTaps?: AnalyserNode[] };

export async function installDestinationTap(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const taps = new WeakMap<BaseAudioContext, AnalyserNode>();
		const all: AnalyserNode[] = [];
		(window as DestTapWindow).__destTaps = all;
		const originalConnect = AudioNode.prototype.connect as (
			this: AudioNode,
			destination: AudioNode | AudioParam,
			output?: number,
			input?: number
		) => AudioNode | void;
		function tapFor(context: BaseAudioContext): AnalyserNode {
			let tap = taps.get(context);
			if (tap === undefined) {
				tap = context.createAnalyser();
				tap.fftSize = 2048;
				taps.set(context, tap);
				all.push(tap);
			}
			return tap;
		}
		AudioNode.prototype.connect = function (
			this: AudioNode,
			destination: AudioNode | AudioParam,
			output?: number,
			input?: number
		) {
			const result = originalConnect.call(this, destination, output, input);
			if (destination === this.context.destination) {
				originalConnect.call(this, tapFor(this.context), output ?? 0, 0);
			}
			return result;
		} as typeof AudioNode.prototype.connect;
	});
}

export async function readDestinationContextStates(page: Page): Promise<string[]> {
	return page.evaluate(() => {
		const taps = (window as DestTapWindow).__destTaps ?? [];
		if (taps.length === 0) {
			throw new Error('no node was ever connected to an AudioContext.destination');
		}
		return taps.map((tap) => tap.context.state);
	});
}

export async function sampleDestinationRms(
	page: Page,
	opts: { windowMs: number; intervalMs: number }
): Promise<{ maxRms: number; meanRms: number; samples: number; contexts: number }> {
	return page.evaluate(
		async ({ windowMs, intervalMs }) => {
			const taps = (window as DestTapWindow).__destTaps ?? [];
			if (taps.length === 0) {
				throw new Error('no node was ever connected to an AudioContext.destination');
			}
			const running = taps.filter((tap) => tap.context.state === 'running');
			if (running.length === 0) {
				throw new Error(
					`no running AudioContext; states=${taps.map((t) => t.context.state).join(',')}`
				);
			}
			let maxRms = 0;
			let sumRms = 0;
			let samples = 0;
			const deadline = performance.now() + windowMs;
			while (performance.now() < deadline) {
				for (const tap of running) {
					const buffer = new Float32Array(tap.fftSize);
					tap.getFloatTimeDomainData(buffer);
					let sum = 0;
					for (const value of buffer) sum += value * value;
					const rms = Math.sqrt(sum / buffer.length);
					maxRms = Math.max(maxRms, rms);
					sumRms += rms;
					samples += 1;
				}
				await new Promise((resolve) => setTimeout(resolve, intervalMs));
			}
			return { maxRms, meanRms: sumRms / samples, samples, contexts: running.length };
		},
		opts
	);
}
