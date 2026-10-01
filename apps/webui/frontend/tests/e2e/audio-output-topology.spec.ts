/**
 * master12-cue34 output topology, rendered by a real Web Audio graph.
 *
 * The room delay (`master_delay_ms`, CUEOUT-14) aligns the room with the
 * headphones, so it belongs on master 1/2 only. Cue 3/4 already carries its
 * own headphone delay; delaying it again by the room amount makes cue
 * monitoring late and cancels the calibration.
 *
 * Runs under playwright.audio-output-topology.config.ts (no server): the
 * module is bundled here and wired into an OfflineAudioContext with four
 * output channels, then the rendered samples are read back. Nothing is faked:
 * the delays, the channel mixing and the merge are Chromium's own.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 Room delay reaches master 1/2 and never cue 3/4.
 * - ✔︎ ✅ 🎯 The master mute still silences all four channels.
 * - ✔︎ ✅ 🎯 A signal injected at the room delay node stays on the master pair.
 *
 * Acceptance tests:
 *
 * - [if] cue 3/4 arrives late by the room delay [then ⛔️] headphones lag the room split.
 * - [if] master 1/2 arrives undelayed [then ⛔️] the room delay setting does nothing.
 * - [if] the master mute leaves any channel audible [then ⛔️] a muted page makes sound.
 * - [if] a signal entering the room delay node reaches 3/4 [then ⛔️] the calibration chirp leaks into cue.
 */
import { expect, test } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SAMPLE_RATE_HZ = 48_000;
const RENDER_FRAMES = 4_800;
const ROOM_DELAY_FRAMES = 960;

interface ChannelOnset {
	/** First frame whose sample is nonzero, or -1 for a silent channel. */
	frame: number;
	/** That sample's value. */
	value: number;
}

async function topologyBundle(): Promise<string> {
	const result = await build({
		entryPoints: [`${FRONTEND_ROOT}/src/lib/rb/audio-output-topology.ts`],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'outputTopology',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

/** Render the djio graph and report each output channel's first nonzero sample. */
async function renderOnsets(
	page: import('@playwright/test').Page,
	options: { muted: boolean; inject: 'buses' | 'room-delay-node' }
): Promise<ChannelOnset[]> {
	return page.evaluate(
		async ({ sampleRate, frames, delayFrames, muted, inject }) => {
			const topology = (globalThis as unknown as { outputTopology: Record<string, Function> })
				.outputTopology;
			const context = new OfflineAudioContext(4, frames, sampleRate);
			const masterGain = context.createGain();
			const masterMuteGain = context.createGain();
			masterMuteGain.gain.value = muted ? 0 : 1;
			const masterDelay = context.createDelay(1);
			masterDelay.delayTime.value = delayFrames / sampleRate;
			const headphoneDelay = context.createDelay(1);
			headphoneDelay.delayTime.value = 0;
			// The profile comes from the same decision the engine makes, fed the
			// real destination's channel count: a fix that always falls back to
			// stereo fails here.
			const resolution = topology.resolveDjOutputProfile(
				'master12-cue34',
				context.destination.maxChannelCount
			);
			if (resolution.profile !== 'master12-cue34' || resolution.fallback !== null) {
				throw new Error(`a 4-channel output did not resolve to djio: ${JSON.stringify(resolution)}`);
			}
			const wired = topology.wireAudioOutputTopology({
				context,
				routing: null,
				profile: resolution.profile,
				masterGain,
				masterMuteGain,
				masterDelay,
				headphoneDelay
			});
			if (wired.multichannelMonitorActive !== true) throw new Error('djio graph was not wired');
			const impulse = (left: number, right: number | null): AudioBufferSourceNode => {
				const buffer = context.createBuffer(right === null ? 1 : 2, 1, sampleRate);
				buffer.getChannelData(0)[0] = left;
				if (right !== null) buffer.getChannelData(1)[0] = right;
				const source = context.createBufferSource();
				source.buffer = buffer;
				source.start(0);
				return source;
			};
			if (inject === 'buses') {
				impulse(1, 0.5).connect(masterGain);
				impulse(0.25, 0.125).connect(headphoneDelay);
			} else {
				impulse(0.75, null).connect(masterDelay);
			}
			const rendered = await context.startRendering();
			if (rendered.numberOfChannels !== 4) {
				throw new Error(`rendered ${rendered.numberOfChannels} channels, expected 4`);
			}
			return [0, 1, 2, 3].map((channel) => {
				const samples = rendered.getChannelData(channel);
				const frame = samples.findIndex((sample) => sample !== 0);
				return { frame, value: frame === -1 ? 0 : samples[frame] };
			});
		},
		{
			sampleRate: SAMPLE_RATE_HZ,
			frames: RENDER_FRAMES,
			delayFrames: ROOM_DELAY_FRAMES,
			muted: options.muted,
			inject: options.inject
		}
	);
}

test.describe('master12-cue34 output topology', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await topologyBundle() });
	});

	test('room delay lands on master 1/2 and leaves cue 3/4 on time', async ({ page }) => {
		const [masterLeft, masterRight, cueLeft, cueRight] = await renderOnsets(page, {
			muted: false,
			inject: 'buses'
		});
		expect(masterLeft).toEqual({ frame: ROOM_DELAY_FRAMES, value: 1 });
		expect(masterRight).toEqual({ frame: ROOM_DELAY_FRAMES, value: 0.5 });
		expect(cueLeft, 'if late then the room delay is also delaying the headphones').toEqual({
			frame: 0,
			value: 0.25
		});
		expect(cueRight).toEqual({ frame: 0, value: 0.125 });
	});

	test('the master mute silences all four channels', async ({ page }) => {
		const onsets = await renderOnsets(page, { muted: true, inject: 'buses' });
		expect(onsets).toEqual(Array.from({ length: 4 }, () => ({ frame: -1, value: 0 })));
	});

	test('a signal entering the room delay node stays on the master pair', async ({ page }) => {
		const [masterLeft, masterRight, cueLeft, cueRight] = await renderOnsets(page, {
			muted: false,
			inject: 'room-delay-node'
		});
		expect(masterLeft).toEqual({ frame: ROOM_DELAY_FRAMES, value: 0.75 });
		expect(masterRight.frame, 'discrete input: a mono signal is not copied to the right').toBe(-1);
		expect(cueLeft.frame).toBe(-1);
		expect(cueRight.frame).toBe(-1);
	});
});
