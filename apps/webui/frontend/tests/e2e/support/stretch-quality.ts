/**
 * Render half of the stretch-quality harness (lane agentB).
 *
 * Binding spec: `.planning/QUALITY-METHODOLOGY-RECONCILED.md`.
 *
 * Rendering happens in REAL Chromium against `OfflineAudioContext` at 44100 Hz
 * with the repo's exact `STRETCH_NODE_OPTIONS`, driving the pinned
 * signalsmith-stretch 1.3.2 from `node_modules`. The module is served to a
 * synthetic origin through Playwright request interception, so this needs no
 * dev server, no port and no data dir. Only fixture EXTRACTION needs the
 * daemon, and that has already happened by the time this runs.
 *
 * Decoding is deliberately in-browser (`decodeAudioData`), which is the same
 * decoder the deck uses, so the excerpt under measurement is the audio the app
 * would actually play rather than a second decoder's opinion of it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 Never trim a render by `node.latency()` (amendment 3). The
 *     self-report is READ and carried as metadata only.
 *     [if] any render is trimmed by the self-report [then ⛔️] every metric is
 *       poisoned by 120 ms of offset
 *   ✔︎ ✅ 🎯 Determinism is ENFORCED, not merely recorded.
 *     [if] two identical renders differ [then ⛔️] the run fails
 *   ✔︎ ✅ 🎯 Channel DISTINCTNESS, never channel count.
 *     [if] a render duplicates its last channel [then ⛔️] it passes a count check
 */
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { Page, Route } from '@playwright/test';

import { STRETCH_NODE_OPTIONS } from '../../../src/lib/rb/stretch-adapter';

/** Synthetic origin. Nothing is ever actually fetched from the network. */
export const SYNTHETIC_ORIGIN = 'https://stretch-quality.invalid';
export const SAMPLE_RATE_HZ = 44_100;

export interface FixturePlan {
	id: string;
	material_class: string;
	stable_id: string;
	offset_s: number;
	duration_s: number;
	bpm: number;
	pitched: boolean;
	source_path: string;
}

export interface ArmPlan {
	id: string;
	block_ms: number | null;
	preset: string | null;
	label: string;
	is_baseline?: boolean;
	is_anchor?: boolean;
}

export interface ConditionPlan {
	id: string;
	rate: number;
	semitones: number;
	role: string;
}

export interface RenderPlan {
	sample_rate_hz: number;
	out_dir: string;
	fixtures: FixturePlan[];
	arms: ArmPlan[];
	conditions: ConditionPlan[];
}

export interface ExcerptResult {
	frames: number;
	channels: number;
	rms_dbfs: number;
	peak_dbfs: number;
	channels_bit_identical: boolean;
	sha256: string;
	base64: string;
}

export interface CellRender {
	sha256: string;
	base64: string;
	frames: number;
	channels: number;
	channels_bit_identical: boolean;
	node_latency_ms: number;
	deterministic: boolean;
	repeat_sha256: string;
}

/** Serve the vendored module and the fetched fixture bodies to the page. */
export async function interceptSyntheticOrigin(page: Page, plan: RenderPlan): Promise<void> {
	const moduleSource = readFileSync(
		join(process.cwd(), 'node_modules/signalsmith-stretch/SignalsmithStretch.mjs'),
		'utf8'
	);
	const sources = new Map(plan.fixtures.map((fixture) => [fixture.id, fixture.source_path]));

	await page.route(`${SYNTHETIC_ORIGIN}/**`, (route: Route) => {
		const path = new URL(route.request().url()).pathname;
		if (path === '/' || path === '/index.html') {
			return route.fulfill({
				status: 200,
				contentType: 'text/html; charset=utf-8',
				body: '<!doctype html><meta charset="utf-8"><title>stretch quality</title><body></body>'
			});
		}
		if (path === '/SignalsmithStretch.mjs') {
			return route.fulfill({
				status: 200,
				contentType: 'text/javascript; charset=utf-8',
				body: moduleSource
			});
		}
		const sourceMatch = /^\/sources\/(.+)$/.exec(path);
		if (sourceMatch) {
			const file = sources.get(decodeURIComponent(sourceMatch[1]));
			if (file === undefined) return route.fulfill({ status: 404, body: 'unknown fixture' });
			return route.fulfill({ status: 200, contentType: 'audio/mpeg', body: readFileSync(file) });
		}
		return route.fulfill({ status: 404, body: 'not served' });
	});
}

/** Install the page-side render helpers. Must run after navigation. */
export async function installRenderHelpers(page: Page): Promise<void> {
	await page.evaluate(async (nodeOptions) => {
		const module = await import(`${location.origin}/SignalsmithStretch.mjs`);
		const create = module.default;
		const scope = window as unknown as Record<string, unknown>;
		scope.__stretchNodeOptions = nodeOptions;

		const toBase64 = (bytes: Uint8Array): string => {
			let binary = '';
			const chunk = 0x8000;
			for (let index = 0; index < bytes.length; index += chunk) {
				binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
			}
			return btoa(binary);
		};

		const interleave = (channels: Float32Array[]): Uint8Array => {
			const frames = channels[0].length;
			const out = new Float32Array(frames * channels.length);
			for (let channel = 0; channel < channels.length; channel += 1) {
				const source = channels[channel];
				for (let frame = 0; frame < frames; frame += 1) {
					out[frame * channels.length + channel] = source[frame];
				}
			}
			return new Uint8Array(out.buffer);
		};

		const sha256 = async (bytes: Uint8Array): Promise<string> => {
			const digest = await crypto.subtle.digest('SHA-256', bytes.buffer as ArrayBuffer);
			return Array.from(new Uint8Array(digest))
				.map((value) => value.toString(16).padStart(2, '0'))
				.join('');
		};

		const bitIdentical = (channels: Float32Array[]): boolean => {
			if (channels.length < 2) return true;
			for (let channel = 1; channel < channels.length; channel += 1) {
				if (channels[channel].length !== channels[0].length) return false;
				for (let frame = 0; frame < channels[0].length; frame += 1) {
					if (channels[channel][frame] !== channels[0][frame]) return false;
				}
			}
			return true;
		};

		const dbfs = (value: number): number => (value <= 0 ? -Infinity : 20 * Math.log10(value));

		/** Decode the fixture body and cut its excerpt window. */
		scope.__decodeExcerpt = async (
			fixtureId: string,
			offsetSeconds: number,
			durationSeconds: number,
			sampleRate: number
		) => {
			const response = await fetch(`${location.origin}/sources/${encodeURIComponent(fixtureId)}`);
			if (!response.ok) throw new Error(`fixture ${fixtureId} served ${response.status}`);
			const encoded = await response.arrayBuffer();
			const decoder = new OfflineAudioContext(2, 1, sampleRate);
			const decoded = await decoder.decodeAudioData(encoded);
			const start = Math.round(offsetSeconds * decoded.sampleRate);
			const frames = Math.round(durationSeconds * decoded.sampleRate);
			if (start + frames > decoded.length) {
				throw new Error(
					`fixture ${fixtureId}: excerpt [${offsetSeconds}, ${offsetSeconds + durationSeconds}) ` +
						`runs past the ${(decoded.length / decoded.sampleRate).toFixed(2)} s decode`
				);
			}
			const channels: Float32Array[] = [];
			for (let channel = 0; channel < 2; channel += 1) {
				const source = decoded.getChannelData(Math.min(channel, decoded.numberOfChannels - 1));
				channels.push(source.slice(start, start + frames));
			}
			scope.__excerpt = channels;

			let sumSquares = 0;
			let peak = 0;
			for (let frame = 0; frame < frames; frame += 1) {
				const mono = (channels[0][frame] + channels[1][frame]) / 2;
				sumSquares += mono * mono;
				peak = Math.max(peak, Math.abs(mono));
			}
			const bytes = interleave(channels);
			return {
				frames,
				channels: 2,
				rms_dbfs: dbfs(Math.sqrt(sumSquares / frames)),
				peak_dbfs: dbfs(peak),
				channels_bit_identical: bitIdentical(channels),
				sha256: await sha256(bytes),
				base64: toBase64(bytes)
			};
		};

		/** One pass through the stretcher. Never trims by node.latency(). */
		const renderPass = async (
			input: Float32Array[],
			outputFrames: number,
			sampleRate: number,
			blockMs: number | null,
			preset: string | null,
			rate: number,
			semitones: number
		): Promise<{ channels: Float32Array[]; latencyMs: number }> => {
			const context = new OfflineAudioContext({
				numberOfChannels: 2,
				sampleRate,
				length: outputFrames
			});
			const node = await create(context, scope.__stretchNodeOptions);
			// The baseline arm NEVER calls configure(): that is the
			// STRETCH_BLOCK_MS null path, byte-identical to pre-round-2.
			if (blockMs !== null) await node.configure({ blockMs });
			else if (preset !== null) await node.configure({ preset });
			const latencySeconds = await node.latency();
			const copies = input.map((channel) => channel.slice());
			await node.addBuffers(
				copies,
				copies.map((channel) => channel.buffer)
			);
			node.connect(context.destination);
			await node.schedule({
				output: 0,
				outputTime: 0,
				active: true,
				input: 0,
				rate,
				semitones
			});
			const rendered = await context.startRendering();
			const channels: Float32Array[] = [];
			for (let channel = 0; channel < rendered.numberOfChannels; channel += 1) {
				channels.push(rendered.getChannelData(channel).slice());
			}
			return { channels, latencyMs: latencySeconds * 1000 };
		};

		/**
		 * Round trip: rate R then 1/R, semitones S then -S, so the output is
		 * time-aligned with the reference and directly comparable. The forward
		 * pass is returned separately for the pitched fixtures, because a round
		 * trip restores the original pitch by construction and would measure
		 * nothing (amendment 8).
		 */
		scope.__renderCell = async (
			sampleRate: number,
			blockMs: number | null,
			preset: string | null,
			rate: number,
			semitones: number,
			wantForward: boolean
		) => {
			const excerpt = scope.__excerpt as Float32Array[];
			const inputFrames = excerpt[0].length;
			const forwardFrames = Math.ceil(inputFrames / rate);

			const runOnce = async () => {
				const forward = await renderPass(
					excerpt,
					forwardFrames,
					sampleRate,
					blockMs,
					preset,
					rate,
					semitones
				);
				const round = await renderPass(
					forward.channels,
					inputFrames,
					sampleRate,
					blockMs,
					preset,
					1 / rate,
					-semitones
				);
				return { forward, round };
			};

			const first = await runOnce();
			const second = await runOnce();
			const firstBytes = interleave(first.round.channels);
			const secondBytes = interleave(second.round.channels);
			const firstDigest = await sha256(firstBytes);
			const secondDigest = await sha256(secondBytes);
			if (firstDigest !== secondDigest) {
				throw new Error(
					`non-deterministic render: ${firstDigest} then ${secondDigest}. Every metric ` +
						'downstream assumes a repeatable instrument, so this fails the run.'
				);
			}

			const result: Record<string, unknown> = {
				sha256: firstDigest,
				base64: toBase64(firstBytes),
				frames: first.round.channels[0].length,
				channels: first.round.channels.length,
				channels_bit_identical: bitIdentical(first.round.channels),
				node_latency_ms: first.round.latencyMs,
				deterministic: true,
				repeat_sha256: secondDigest
			};
			if (wantForward) {
				const forwardBytes = interleave(first.forward.channels);
				result.forward = {
					sha256: await sha256(forwardBytes),
					base64: toBase64(forwardBytes),
					frames: first.forward.channels[0].length,
					channels: first.forward.channels.length
				};
			}
			return result;
		};
	}, STRETCH_NODE_OPTIONS as unknown as Record<string, unknown>);
}

/** Write a base64 interleaved-f32 payload plus the sidecar Python re-verifies. */
export function writePcm(
	path: string,
	base64: string,
	sampleRateHz: number,
	channels: number,
	frames: number
): string {
	const payload = Buffer.from(base64, 'base64');
	const digest = createHash('sha256').update(payload).digest('hex');
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, payload);
	writeFileSync(
		`${path}.json`,
		`${JSON.stringify({ channels, frames, sample_rate_hz: sampleRateHz, sha256: digest }, null, 2)}\n`
	);
	return digest;
}
