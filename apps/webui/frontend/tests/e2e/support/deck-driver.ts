/**
 * Headless deck-state driver for Playwright performance e2e (issue #10408).
 *
 * House rule (Mon 12 Oct 2026): UI and acceptance tests drive the app through
 * its agent surface, never by store injection, and tear down state. Pads epic
 * #9963 and mixer epic #10077 build on this helper.
 *
 * Commands go through the same performance IPC validator agents hit
 * (`window.musicDjToolsPerformance.dispatch`). `viaHttp` sends matching single
 * orders to POST /api/v1/commands and waits on the ui-mirror ticker.
 *
 * `beat_loop` and `beat_jump` need a PQTZ grid on the loaded track; the default
 * fixture may have none unless OWN_BEATGRID_SEEDED (see playwright.performance.config.ts).
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test';

import type { DeckId } from '../../../src/lib/rb/deck-slots';
import type { HotCueSlot } from '../../../src/lib/rb/hot-cue-types';
import type { EqBand } from '../../../src/lib/rb/mixer-types';
import type {
	PerformanceCommand,
	PerformanceDeckSnapshot,
	PerformanceState
} from '../../../src/lib/rb/performance-ipc.svelte';

import { waitForPerformanceIpc } from './performance-ready';

export const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';

const DEFAULT_TIMEOUT_MS = 8_000;

const SETTLE_INTERVALS = [25, 50, 100, 200] as const;

export async function queryPerformance(page: Page): Promise<PerformanceState> {
	await waitForPerformanceIpc(page);
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

export async function dispatchPerformance(
	page: Page,
	command: PerformanceCommand
): Promise<PerformanceState> {
	await waitForPerformanceIpc(page);
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

interface MirrorDeck {
	stable_id: string | null;
	playing: boolean;
	position: { ms: number };
}

interface UiMirrorBody {
	decks: Record<string, MirrorDeck>;
	client_open?: boolean;
}

interface AgentOrderResponse {
	steps: Array<{ status: string; error?: string }>;
}

// ----- crossfader restore (first driver that moves it owns teardown)

let crossfaderBaseline: number | null = null;
let crossfaderOwnerDeck: DeckId | null = null;

async function settle<T>(
	read: () => Promise<T>,
	done: (v: T) => boolean,
	deck: DeckId,
	verb: string,
	field: string,
	expectation: string,
	timeoutMs: number
): Promise<void> {
	try {
		await expect
			.poll(async () => done(await read()), {
				message: `deck ${deck} ${verb}: ${field} ${expectation}`,
				timeout: timeoutMs,
				intervals: [...SETTLE_INTERVALS]
			})
			.toBe(true);
	} catch {
		const last = await read();
		throw new Error(
			`deck ${deck} ${verb}: ${field} never ${expectation} within ${timeoutMs} ms (last ${JSON.stringify(last)})`
		);
	}
}

function hotCueRow(
	snapshot: PerformanceDeckSnapshot,
	slot: HotCueSlot
): { slot: HotCueSlot; cue: { in_ms: number } | null; revision: string } | undefined {
	return snapshot.hot_cue_slots.find((row) => row.slot === slot);
}

export interface DeckDriver {
	load(stable_id: string): Promise<PerformanceDeckSnapshot>;
	seekMs(ms: number): Promise<PerformanceDeckSnapshot>;
	play(): Promise<PerformanceDeckSnapshot>;
	pause(): Promise<PerformanceDeckSnapshot>;
	setHotCue(slot: HotCueSlot, in_ms?: number): Promise<PerformanceDeckSnapshot>;
	triggerHotCue(slot: HotCueSlot): Promise<PerformanceDeckSnapshot>;
	loop(in_ms: number, out_ms: number): Promise<PerformanceDeckSnapshot>;
	beatLoop(beats: number): Promise<PerformanceDeckSnapshot>;
	loopExit(): Promise<PerformanceDeckSnapshot>;
	beatJump(beats: number): Promise<PerformanceDeckSnapshot>;
	quantize(on: boolean): Promise<PerformanceDeckSnapshot>;
	fader(x: number): Promise<PerformanceDeckSnapshot>;
	eq(band: EqBand, x: number): Promise<PerformanceDeckSnapshot>;
	crossfader(x: number): Promise<PerformanceDeckSnapshot>;
	snapshot(): Promise<PerformanceDeckSnapshot>;
	/** Poll until `done` passes or the driver timeout; used by harness self-tests. */
	awaitSnapshot(
		verb: string,
		field: string,
		expectation: string,
		done: (snapshot: PerformanceDeckSnapshot) => boolean
	): Promise<void>;
	teardown(): Promise<void>;
}

export function deckDriver(page: Page, deck: DeckId, options?: { timeoutMs?: number }): DeckDriver {
	const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	let touched = false;
	const savedHotCueSlots = new Set<HotCueSlot>();
	const mixerBaseline: Partial<{
		fader: number;
		eq_low: number;
		eq_mid: number;
		eq_high: number;
	}> = {};
	let quantizeBaseline: boolean | null = null;

	function markTouched(): void {
		touched = true;
	}

	async function deckSnap(): Promise<PerformanceDeckSnapshot> {
		return (await queryPerformance(page)).decks[deck];
	}

	const driver: DeckDriver = {
		async load(stable_id: string): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const pre = await deckSnap();
			const command: PerformanceCommand = pre.playing
				? { type: 'load', deck, stable_id, replace_playing: true }
				: { type: 'load', deck, stable_id };
			await dispatchPerformance(page, command);
			await settle(
				deckSnap,
				(s) => s.stable_id === stable_id && (s.duration_ms ?? 0) > 0 && !s.transport_pending,
				deck,
				'load',
				'stable_id',
				`equal ${stable_id} with duration and idle transport`,
				timeoutMs
			);
			return deckSnap();
		},

		async seekMs(ms: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, { type: 'seek', deck, position_ms: ms, exact: true });
			await settle(
				deckSnap,
				(s) => Math.abs(s.position_ms - ms) <= 50,
				deck,
				'seekMs',
				'position_ms',
				`within 50 ms of ${ms}`,
				timeoutMs
			);
			return deckSnap();
		},

		async play(): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, { type: 'play', deck, playing: true });
			await settle(
				deckSnap,
				(s) => s.playing && !s.transport_pending,
				deck,
				'play',
				'playing',
				'true with idle transport',
				timeoutMs
			);
			return deckSnap();
		},

		async pause(): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, { type: 'play', deck, playing: false });
			await settle(
				deckSnap,
				(s) => !s.playing && !s.transport_pending,
				deck,
				'pause',
				'playing',
				'false with idle transport',
				timeoutMs
			);
			return deckSnap();
		},

		async setHotCue(slot: HotCueSlot, in_ms?: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const pre = await deckSnap();
			const targetMs = in_ms ?? Math.round(pre.position_ms);
			const row = hotCueRow(pre, slot);
			if (row === undefined || row.revision.length === 0) {
				throw new Error(`deck ${deck} setHotCue: missing revision for slot ${slot}`);
			}
			await dispatchPerformance(page, {
				type: 'hot_cue_save',
				deck,
				slot,
				in_ms: targetMs,
				revision: row.revision
			});
			savedHotCueSlots.add(slot);
			await settle(
				deckSnap,
				(s) => hotCueRow(s, slot)?.cue?.in_ms === targetMs,
				deck,
				'setHotCue',
				`hot_cue_slots[${slot}].cue.in_ms`,
				`equal ${targetMs}`,
				timeoutMs
			);
			return deckSnap();
		},

		async triggerHotCue(slot: HotCueSlot): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const pre = await deckSnap();
			const row = hotCueRow(pre, slot);
			if (row?.cue === null || row?.cue === undefined) {
				throw new Error(`deck ${deck} triggerHotCue: slot ${slot} is empty`);
			}
			const cueMs = row.cue.in_ms;
			await dispatchPerformance(page, { type: 'hot_cue_trigger', deck, slot });
			await settle(
				deckSnap,
				(s) =>
					s.hot_cue_armed === null && s.position_ms >= cueMs - 50 && s.position_ms <= cueMs + 1500,
				deck,
				'triggerHotCue',
				'position_ms',
				`near hot cue ${cueMs}`,
				timeoutMs
			);
			return deckSnap();
		},

		async loop(in_ms: number, out_ms: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, {
				type: 'loop',
				deck,
				loop: { in_ms, out_ms },
				quantize: false
			});
			await settle(
				deckSnap,
				(s) =>
					s.loop_engaged &&
					s.loop !== null &&
					Math.abs(s.loop.in_ms - in_ms) <= 1 &&
					Math.abs(s.loop.out_ms - out_ms) <= 1,
				deck,
				'loop',
				'loop.in_ms / loop.out_ms',
				`engaged at ${in_ms}-${out_ms}`,
				timeoutMs
			);
			return deckSnap();
		},

		async beatLoop(beats: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, { type: 'beat_loop', deck, beats });
			await settle(
				deckSnap,
				(s) => s.loop_engaged && s.loop !== null && s.loop.beat_length === beats,
				deck,
				'beatLoop',
				'loop.beat_length',
				`equal ${beats}`,
				timeoutMs
			);
			return deckSnap();
		},

		async loopExit(): Promise<PerformanceDeckSnapshot> {
			markTouched();
			await dispatchPerformance(page, { type: 'loop', deck, loop: null });
			await settle(
				deckSnap,
				(s) => !s.loop_engaged,
				deck,
				'loopExit',
				'loop_engaged',
				'false',
				timeoutMs
			);
			return deckSnap();
		},

		async beatJump(beats: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const before = (await deckSnap()).position_ms;
			await dispatchPerformance(page, { type: 'beat_jump', deck, beats });
			const sign = beats > 0 ? 1 : beats < 0 ? -1 : 0;
			await settle(
				deckSnap,
				(s) => (s.position_ms - before) * sign > 1,
				deck,
				'beatJump',
				'position_ms',
				`moved ${sign > 0 ? 'forward' : 'backward'} from ${before}`,
				timeoutMs
			);
			return deckSnap();
		},

		async quantize(on: boolean): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const state = await queryPerformance(page);
			if (quantizeBaseline === null) {
				quantizeBaseline = state.decks[deck].quantize_enabled;
			}
			const command: PerformanceCommand = on
				? { type: 'quantize', deck, enabled: true }
				: { type: 'quantize', deck, enabled: false, by_user: true };
			await dispatchPerformance(page, command);
			await settle(
				deckSnap,
				(s) => s.quantize_enabled === on,
				deck,
				'quantize',
				'quantize_enabled',
				`equal ${on}`,
				timeoutMs
			);
			return deckSnap();
		},

		async fader(x: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const state = await queryPerformance(page);
			if (mixerBaseline.fader === undefined) {
				mixerBaseline.fader = state.mixer.channels[deck].fader;
			}
			await dispatchPerformance(page, { type: 'fader', deck, value: x });
			await settle(
				async () => (await queryPerformance(page)).mixer.channels[deck].fader,
				(v) => Math.abs(v - x) <= 1e-3,
				deck,
				'fader',
				`mixer.channels[${deck}].fader`,
				`near ${x}`,
				timeoutMs
			);
			return deckSnap();
		},

		async eq(band: EqBand, x: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			const state = await queryPerformance(page);
			const key = `eq_${band}` as 'eq_low' | 'eq_mid' | 'eq_high';
			if (mixerBaseline[key] === undefined) {
				mixerBaseline[key] = state.mixer.channels[deck][key];
			}
			await dispatchPerformance(page, { type: 'eq', deck, band, value: x });
			await settle(
				async () => (await queryPerformance(page)).mixer.channels[deck][key],
				(v) => Math.abs(v - x) <= 1e-3,
				deck,
				'eq',
				`mixer.channels[${deck}].${key}`,
				`near ${x}`,
				timeoutMs
			);
			return deckSnap();
		},

		async crossfader(x: number): Promise<PerformanceDeckSnapshot> {
			markTouched();
			if (crossfaderOwnerDeck === null) {
				const state = await queryPerformance(page);
				crossfaderBaseline = state.mixer.crossfader;
				crossfaderOwnerDeck = deck;
			}
			await dispatchPerformance(page, { type: 'crossfader', value: x });
			await settle(
				async () => (await queryPerformance(page)).mixer.crossfader,
				(v) => Math.abs(v - x) <= 1e-3,
				deck,
				'crossfader',
				'mixer.crossfader',
				`near ${x}`,
				timeoutMs
			);
			return deckSnap();
		},

		async snapshot(): Promise<PerformanceDeckSnapshot> {
			return deckSnap();
		},

		async awaitSnapshot(
			verb: string,
			field: string,
			expectation: string,
			done: (snapshot: PerformanceDeckSnapshot) => boolean
		): Promise<void> {
			markTouched();
			await settle(deckSnap, done, deck, verb, field, expectation, timeoutMs);
		},

		async teardown(): Promise<void> {
			if (!touched) return;
			const errors: string[] = [];

			try {
				const snap = await deckSnap();
				if (snap.playing) {
					await driver.pause();
				}
			} catch (error) {
				errors.push(`pause: ${String(error)}`);
			}

			for (const slot of savedHotCueSlots) {
				try {
					const snap = await deckSnap();
					const row = hotCueRow(snap, slot);
					if (row !== undefined && row.cue !== null && row.revision.length > 0) {
						await dispatchPerformance(page, {
							type: 'hot_cue_clear',
							deck,
							slot,
							revision: row.revision
						});
					}
				} catch (error) {
					errors.push(`hot_cue_clear ${slot}: ${String(error)}`);
				}
			}
			savedHotCueSlots.clear();

			if (quantizeBaseline !== null) {
				try {
					const snap = await deckSnap();
					if (snap.quantize_enabled !== quantizeBaseline) {
						await driver.quantize(quantizeBaseline);
					}
				} catch (error) {
					errors.push(`quantize restore: ${String(error)}`);
				}
			}

			const mixerKeys = ['fader', 'eq_low', 'eq_mid', 'eq_high'] as const;
			for (const key of mixerKeys) {
				const baseline = mixerBaseline[key];
				if (baseline === undefined) continue;
				try {
					if (key === 'fader') {
						const cur = (await queryPerformance(page)).mixer.channels[deck].fader;
						if (Math.abs(cur - baseline) > 1e-3) {
							await dispatchPerformance(page, { type: 'fader', deck, value: baseline });
						}
					} else {
						const band = key.replace('eq_', '') as EqBand;
						const cur = (await queryPerformance(page)).mixer.channels[deck][key];
						if (Math.abs(cur - baseline) > 1e-3) {
							await dispatchPerformance(page, { type: 'eq', deck, band, value: baseline });
						}
					}
				} catch (error) {
					errors.push(`${key} restore: ${String(error)}`);
				}
			}

			if (crossfaderOwnerDeck === deck && crossfaderBaseline !== null) {
				try {
					const cur = (await queryPerformance(page)).mixer.crossfader;
					if (Math.abs(cur - crossfaderBaseline) > 1e-3) {
						await dispatchPerformance(page, { type: 'crossfader', value: crossfaderBaseline });
					}
					crossfaderOwnerDeck = null;
					crossfaderBaseline = null;
				} catch (error) {
					errors.push(`crossfader restore: ${String(error)}`);
				}
			}

			try {
				await dispatchPerformance(page, { type: 'unload', deck });
				await settle(
					deckSnap,
					(s) => s.stable_id === null,
					deck,
					'teardown',
					'stable_id',
					'null after unload',
					timeoutMs
				);
			} catch (error) {
				errors.push(`unload: ${String(error)}`);
			}

			touched = false;
			quantizeBaseline = null;
			if (errors.length > 0) {
				throw new Error(`deck ${deck} teardown failed: ${errors.join('; ')}`);
			}
		}
	};

	return driver;
}

async function fetchUiMirror(request: APIRequestContext): Promise<UiMirrorBody> {
	const response = await request.get(`${API_BASE}/api/v1/state/ui-mirror`);
	const text = await response.text();
	if (response.status() === 409) {
		const body = JSON.parse(text) as { client_open?: boolean };
		if (body.client_open === false) {
			throw new Error('POST /api/v1/commands: client_open false (no performance page)');
		}
	}
	if (!response.ok()) {
		throw new Error(`ui-mirror HTTP ${response.status()}: ${text}`);
	}
	return JSON.parse(text) as unknown as UiMirrorBody;
}

async function postSingleOrder(
	request: APIRequestContext,
	command: PerformanceCommand
): Promise<void> {
	const response = await request.post(`${API_BASE}/api/v1/commands`, {
		data: { single: command }
	});
	const text = await response.text();
	if (response.status() === 409) {
		const body = JSON.parse(text) as { client_open?: boolean };
		if (body.client_open === false) {
			throw new Error('POST /api/v1/commands: client_open false (no performance page)');
		}
		throw new Error(`POST /api/v1/commands HTTP 409: ${text}`);
	}
	if (!response.ok()) {
		throw new Error(`POST /api/v1/commands HTTP ${response.status()}: ${text}`);
	}
	const body = JSON.parse(text) as unknown as AgentOrderResponse;
	for (let i = 0; i < body.steps.length; i++) {
		const step = body.steps[i];
		if (step.status !== 'succeeded') {
			throw new Error(
				`POST /api/v1/commands step ${i} ${step.status}${step.error ? `: ${step.error}` : ''}`
			);
		}
	}
}

export interface HttpDeckDriver {
	load(stable_id: string): Promise<MirrorDeck>;
	play(): Promise<MirrorDeck>;
	pause(): Promise<MirrorDeck>;
	seekMs(ms: number): Promise<MirrorDeck>;
	mirrorDeck(): Promise<MirrorDeck>;
	teardown(): Promise<void>;
}

export function viaHttp(
	request: APIRequestContext,
	deck: DeckId,
	options?: { timeoutMs?: number }
): HttpDeckDriver {
	const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	let touched = false;

	async function mirrorDeck(): Promise<MirrorDeck> {
		const mirror = await fetchUiMirror(request);
		const row = mirror.decks[String(deck)];
		if (row === undefined) {
			throw new Error(`mirror.decks[${deck}] missing`);
		}
		return row;
	}

	const http: HttpDeckDriver = {
		async load(stable_id: string): Promise<MirrorDeck> {
			touched = true;
			const pre = await mirrorDeck();
			const command: PerformanceCommand = pre.playing
				? { type: 'load', deck, stable_id, replace_playing: true }
				: { type: 'load', deck, stable_id };
			await postSingleOrder(request, command);
			await settle(
				mirrorDeck,
				(m) => m.stable_id === stable_id,
				deck,
				'load',
				`mirror.decks[${deck}].stable_id`,
				`equal ${stable_id}`,
				timeoutMs
			);
			return mirrorDeck();
		},

		async play(): Promise<MirrorDeck> {
			touched = true;
			await postSingleOrder(request, { type: 'play', deck, playing: true });
			await settle(
				mirrorDeck,
				(m) => m.playing === true,
				deck,
				'play',
				`mirror.decks[${deck}].playing`,
				'true',
				timeoutMs
			);
			return mirrorDeck();
		},

		async pause(): Promise<MirrorDeck> {
			touched = true;
			await postSingleOrder(request, { type: 'play', deck, playing: false });
			await settle(
				mirrorDeck,
				(m) => m.playing === false,
				deck,
				'pause',
				`mirror.decks[${deck}].playing`,
				'false',
				timeoutMs
			);
			return mirrorDeck();
		},

		async seekMs(ms: number): Promise<MirrorDeck> {
			touched = true;
			await postSingleOrder(request, { type: 'seek', deck, position_ms: ms, exact: true });
			await settle(
				mirrorDeck,
				(m) => Math.abs(m.position.ms - ms) <= 50,
				deck,
				'seekMs',
				`mirror.decks[${deck}].position.ms`,
				`within 50 ms of ${ms}`,
				timeoutMs
			);
			return mirrorDeck();
		},

		mirrorDeck,

		async teardown(): Promise<void> {
			if (!touched) return;
			try {
				const pre = await mirrorDeck();
				if (pre.playing) {
					await http.pause();
				}
			} catch {
				// best effort before unload
			}
			await postSingleOrder(request, { type: 'unload', deck });
			await settle(
				mirrorDeck,
				(m) => m.stable_id === null,
				deck,
				'teardown',
				`mirror.decks[${deck}].stable_id`,
				'null after unload',
				timeoutMs
			);
			touched = false;
		}
	};

	return http;
}

export function expectDeck(driver: { snapshot(): Promise<PerformanceDeckSnapshot> }): {
	toBePausedAt: (ms: number, toleranceMs?: number) => Promise<void>;
	toBeLooping: (in_ms: number, out_ms: number, toleranceMs?: number) => Promise<void>;
	toBePlaying: () => Promise<void>;
} {
	return {
		async toBePausedAt(ms: number, toleranceMs = 50): Promise<void> {
			await expect
				.poll(
					async () => {
						const s = await driver.snapshot();
						return {
							playing: s.playing,
							near: Math.abs(s.position_ms - ms) <= toleranceMs
						};
					},
					{
						message: `deck paused at ${ms} +/- ${toleranceMs} ms (playing, position_ms)`,
						intervals: [...SETTLE_INTERVALS]
					}
				)
				.toEqual({ playing: false, near: true });
		},

		async toBeLooping(in_ms: number, out_ms: number, toleranceMs = 1): Promise<void> {
			await expect
				.poll(
					async () => {
						const s = await driver.snapshot();
						return (
							s.loop_engaged &&
							s.loop !== null &&
							Math.abs(s.loop.in_ms - in_ms) <= toleranceMs &&
							Math.abs(s.loop.out_ms - out_ms) <= toleranceMs
						);
					},
					{ message: `deck looping ${in_ms}-${out_ms}`, intervals: [...SETTLE_INTERVALS] }
				)
				.toBe(true);
		},

		async toBePlaying(): Promise<void> {
			await expect
				.poll(
					async () => {
						const s = await driver.snapshot();
						return s.playing && !s.transport_pending;
					},
					{ message: 'deck playing with idle transport', intervals: [...SETTLE_INTERVALS] }
				)
				.toBe(true);
		}
	};
}
