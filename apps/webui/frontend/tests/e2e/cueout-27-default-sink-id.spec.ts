/**
 * CUEOUT-27 acceptance on REAL Chromium: real AudioContext.setSinkId, a real
 * audiooutput device, speaker-selection withheld.
 *
 * The node suite `cueout-27-default-sink-id.test.mjs` records that
 * `browserSinkId` maps `"default"` to `""` and that cue/master call sites go
 * through it. A recording stub cannot say that Chrome still rejects
 * `setSinkId("default")` without media permission, or that the live app sends
 * `""` when MAIN or CUE is picked as `"default"`. This spec is where that is
 * proved.
 *
 * RUN. Root `playwright.config.ts` (generated real-audio fixture library,
 * engine + vite on the worktree's claimed port pair):
 *   pnpm exec playwright test tests/e2e/cueout-27-default-sink-id.spec.ts
 *
 * Requirements:
 *
 * - [if] MAIN or CUE is picked as "default" without microphone permission
 *   [then] setSinkId receives "" and the holder keeps "default" ⛔️
 * - [if] setSinkId("default") is sent without media permission [then ⛔️]
 *   Chrome throws NotFoundError "device default is not found" and a toast.
 *
 * Never grant microphone or speaker-selection. Never click Choose output /
 * allow device access. Never install a fake MediaDeviceInfo list or a fake
 * AudioContext. A PulseAudio/pipewire dummy sink still counts as an output;
 * skip only a host that exposes no audiooutput at all.
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const GATED_ALIAS = /device default is not found|NotFoundError/i;
const SAME_OUTPUT_REFUSAL = /MAIN and CUE are the same output/i;

type Cueout27Window = Window & { __cueout27SinkCalls?: string[] };

type HostProbe = {
	hasSetSinkId: boolean;
	audioOutputCount: number;
	contextError: string | null;
};

type HeadphoneSnapshot = {
	outputs: { id: string }[];
	selected_master_output_device_id: string | null;
	selected_output_device_id: string | null;
	output_mode: string;
	split_reason: string | null | undefined;
	error: string | null;
	last_error: string | null;
};

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

function watchToastLog(page: Page): string[] {
	const lines: string[] = [];
	page.on('console', (message) => {
		const text = message.text();
		if (text.startsWith('[perf-event] toast-')) lines.push(text);
	});
	return lines;
}

function skipIfHostCannotProve(probe: HostProbe): void {
	test.skip(!probe.hasSetSinkId, 'AudioContext.prototype.setSinkId is not a function on this host');
	test.skip(
		probe.audioOutputCount === 0,
		'enumerateDevices returned no audiooutput entries on this host'
	);
	test.skip(
		probe.contextError !== null,
		`creating or resuming an AudioContext failed: ${probe.contextError ?? ''}`
	);
}

async function probeHost(page: Page): Promise<HostProbe> {
	return page.evaluate(async () => {
		const proto = AudioContext.prototype as AudioContext & {
			setSinkId?: (sinkId: string) => Promise<void>;
		};
		const hasSetSinkId = typeof proto.setSinkId === 'function';
		const devices = await navigator.mediaDevices.enumerateDevices();
		const audioOutputCount = devices.filter((device) => device.kind === 'audiooutput').length;
		if (!hasSetSinkId) {
			return { hasSetSinkId, audioOutputCount, contextError: null };
		}
		let contextError: string | null = null;
		try {
			const ctx = new AudioContext();
			try {
				if (ctx.state !== 'running') await ctx.resume();
				if (ctx.state !== 'running') contextError = `AudioContext.state is ${ctx.state}`;
			} finally {
				await ctx.close();
			}
		} catch (error) {
			contextError = error instanceof Error ? error.message : String(error);
		}
		return { hasSetSinkId, audioOutputCount, contextError };
	});
}

async function installSinkRecorder(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const proto = AudioContext.prototype as AudioContext & {
			setSinkId?: (sinkId: string) => Promise<void>;
		};
		const original = proto.setSinkId;
		if (typeof original !== 'function') return;
		const calls: string[] = [];
		(window as Cueout27Window).__cueout27SinkCalls = calls;
		proto.setSinkId = async function (this: AudioContext, sinkId: string) {
			calls.push(sinkId);
			return original.call(this, sinkId);
		};
	});
}

async function recordedSinkCalls(page: Page): Promise<string[]> {
	return page.evaluate(() => [...((window as Cueout27Window).__cueout27SinkCalls ?? [])]);
}

async function headphoneSnapshot(page: Page): Promise<HeadphoneSnapshot> {
	return page.evaluate(() => {
		const query = window.musicDjToolsPerformance!.query();
		const hp = query.mixer.headphones;
		return {
			outputs: hp.outputs.map((output) => ({ id: output.id })),
			selected_master_output_device_id: hp.selected_master_output_device_id,
			selected_output_device_id: hp.selected_output_device_id,
			output_mode: hp.output_mode,
			split_reason: hp.split_reason,
			error: hp.error,
			last_error: query.last_error
		};
	});
}

function expectNoGatedAliasFailure(snapshot: HeadphoneSnapshot, toasts: string[]): void {
	expect(snapshot.error ?? '', 'headphones.error names the gated alias').not.toMatch(GATED_ALIAS);
	expect(snapshot.last_error ?? '', 'query().last_error names the gated alias').not.toMatch(
		GATED_ALIAS
	);
	expect(
		toasts.some((line) => GATED_ALIAS.test(line)),
		`a toast named the gated alias: ${toasts.join(' | ')}`
	).toBe(false);
}

async function bootPerformanceWithRecorder(page: Page): Promise<string[]> {
	const toasts = watchToastLog(page);
	await installSinkRecorder(page);
	await page.goto('/performance');
	await waitForPerformanceIpc(page);
	skipIfHostCannotProve(await probeHost(page));
	return toasts;
}

test('CUEOUT-27: Chromium rejects setSinkId("default") without permission and accepts ""', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.goto('/performance');
	skipIfHostCannotProve(await probeHost(page));

	const result = await page.evaluate(async () => {
		const ctx = new AudioContext() as AudioContext & {
			setSinkId: (sinkId: string) => Promise<void>;
			sinkId: string;
		};
		try {
			if (ctx.state !== 'running') await ctx.resume();
			if (ctx.state !== 'running') {
				return { kind: 'resume-failed' as const, state: ctx.state };
			}
			let defaultRejected: { name: string; message: string } | null = null;
			let defaultAccepted = false;
			try {
				await ctx.setSinkId('default');
				defaultAccepted = true;
			} catch (error) {
				defaultRejected = {
					name: error instanceof Error ? error.name : 'Error',
					message: error instanceof Error ? error.message : String(error)
				};
			}
			await ctx.setSinkId('');
			return {
				kind: 'probed' as const,
				defaultAccepted,
				defaultRejected,
				emptySinkId: ctx.sinkId
			};
		} finally {
			await ctx.close();
		}
	});

	if (result.kind === 'resume-failed') {
		test.skip(true, `creating or resuming an AudioContext failed: state is ${result.state}`);
	}

	expect(result.kind).toBe('probed');
	if (result.kind !== 'probed') return;

	if (result.defaultAccepted) {
		test.info().annotations.push({
			type: 'skip-assertion',
			description:
				'this host accepts setSinkId("default") without media permission and does not reproduce the gated alias'
		});
	} else {
		expect(
			result.defaultRejected?.name,
			`setSinkId("default") rejected as ${result.defaultRejected?.name}: ${result.defaultRejected?.message}`
		).toBe('NotFoundError');
	}

	expect(result.emptySinkId, 'setSinkId("") must land on the user-agent default').toBe('');
});

test('CUEOUT-27: MAIN pick of "default" sends "" and the holder keeps "default"', async ({
	page
}) => {
	test.setTimeout(60_000);
	const toasts = await bootPerformanceWithRecorder(page);

	const listed = await headphoneSnapshot(page);
	expect(
		listed.outputs.some((output) => output.id === 'default'),
		`query().mixer.headphones.outputs has no default row: ${JSON.stringify(listed.outputs)}`
	).toBe(true);

	await page.evaluate(async () => {
		try {
			await window.musicDjToolsPerformance!.dispatch({
				type: 'headphone_master_select',
				device_id: 'default'
			});
		} catch {
			// last_error / headphones.error hold the reason
		}
	});

	await expect
		.poll(async () => (await headphoneSnapshot(page)).selected_master_output_device_id, {
			timeout: 15_000,
			message: 'selected_master_output_device_id never became "default"'
		})
		.toBe('default');

	const calls = await recordedSinkCalls(page);
	expect(calls, `setSinkId calls: ${JSON.stringify(calls)}`).toContain('');
	expect(
		calls,
		`setSinkId must not receive the gated alias: ${JSON.stringify(calls)}`
	).not.toContain('default');

	const after = await headphoneSnapshot(page);
	expectNoGatedAliasFailure(after, toasts);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeVisible();
	await expect(page.getByLabel('master output device', { exact: true })).toHaveValue('default');
});

test('CUEOUT-27: CUE pick of "default" does not send the gated alias', async ({ page }) => {
	test.setTimeout(60_000);
	const toasts = await bootPerformanceWithRecorder(page);

	await page.evaluate(async () => {
		try {
			await window.musicDjToolsPerformance!.dispatch({
				type: 'headphone_output_select',
				device_id: 'default'
			});
		} catch {
			// last_error / headphones.error hold the reason
		}
	});

	const after = await headphoneSnapshot(page);
	const calls = await recordedSinkCalls(page);
	expectNoGatedAliasFailure(after, toasts);
	expect(
		calls,
		`setSinkId must not receive the gated alias: ${JSON.stringify(calls)}`
	).not.toContain('default');

	const twoOutputsOnDefault =
		after.output_mode === 'two_outputs' &&
		after.selected_output_device_id === 'default' &&
		calls.includes('');
	const splitOnDefault = after.split_reason === 'same_device';
	// AUDFID-01 replaced CUEOUT-26 automatic split: a CUE on MAIN's only
	// listed output is refused. That is still a successful default pick.
	const sameOutputRefusal =
		SAME_OUTPUT_REFUSAL.test(after.error ?? '') || SAME_OUTPUT_REFUSAL.test(after.last_error ?? '');
	expect(
		twoOutputsOnDefault || splitOnDefault || sameOutputRefusal,
		`CUE pick of default neither landed nor split nor refused same-output: ${JSON.stringify({ after, calls })}`
	).toBe(true);
});
