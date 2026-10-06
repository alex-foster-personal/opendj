/**
 * AutoPlay / mixing error hunt (#1853).
 *
 * Leaves the real `/performance` UI running against a 6-track fixture,
 * starts AutoPlay, interleaves seeded mixing, and fails on every error
 * signature that is not an allowlisted open issue. This is not
 * `audio-soak.spec.ts`: no modelled output, no RMS assertion.
 *
 * Two tests, serial, separate pages. The negative control runs first so a
 * broken probe fails in seconds rather than after N minutes. Its injected
 * throw must not land in the main hunt's log.
 */
import { expect, test, type ConsoleMessage, type Page, type Response } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';
import {
	HUNT_DURATION_MS,
	HUNT_HTML_REPORT_DIR,
	HUNT_OUTPUT_DIR,
	FIXTURE_MANIFEST_PATH,
	huntMinutes,
	huntSeed
} from './playwright.autoplay-error-hunt.config';
import {
	buildHuntReport,
	formatReport,
	groupBySignature,
	HUNT_ROUTE,
	loadAllowlist,
	NEGATIVE_CONTROL_NEEDLE,
	type HuntEvent,
	type HuntEventKind,
	unexpectedSignatures,
	writeHuntReport
} from './support/autoplay-error-hunt-report';
import { AUTO_PLAY_IDLE_MS } from '../../src/lib/rb/autoplay-idle';
import { autoplayHuntFixture } from './support/fixture-manifest';

const ALLOW_PATH = fileURLToPath(new URL('./autoplay-error-hunt.allow.json', import.meta.url));
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';
const PAST_GUARD_MS = 700;
const STALL_MS = AUTO_PLAY_IDLE_MS;
const ACTION_INTERVAL_MS = 2_000;
const MASTER_VOLUME = 0.05;
const EQ_BANDS = ['high', 'mid', 'low'] as const;
const MIX_KINDS = ['load-other-deck', 'crossfader', 'eq', 'filter', 'tempo', 'sync'] as const;
const REQUIRED_KINDS = [...MIX_KINDS, 'playlist-switch'] as const;

type MixKind = (typeof MIX_KINDS)[number];
type ActionKind = MixKind | 'playlist-switch';

type StallWatch = {
	idleSince: number | null;
	idleRecorded: boolean;
	bannerRecorded: boolean;
	frozenSince: Record<1 | 2, number | null>;
	frozenRecorded: Record<1 | 2, boolean>;
	lastPos: Record<1 | 2, number | null>;
};

type HuntSession = {
	events: HuntEvent[];
	lastAction: string;
	seenToastIds: Set<string>;
	stall: StallWatch;
};

test.describe.configure({ mode: 'serial' });

function mulberry32(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a += 0x6d2b79f5;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

function buildSchedule(seed: number, durationMs: number): ActionKind[] {
	const rng = mulberry32(seed);
	const count = Math.max(Math.ceil(durationMs / ACTION_INTERVAL_MS), MIX_KINDS.length + 1);
	const first = [...MIX_KINDS];
	for (let i = first.length - 1; i > 0; i -= 1) {
		const j = Math.floor(rng() * (i + 1));
		const current = first[i];
		const swap = first[j];
		if (current === undefined || swap === undefined) continue;
		first[i] = swap;
		first[j] = current;
	}
	const schedule: ActionKind[] = [...first];
	while (schedule.length < count) {
		const pick = MIX_KINDS[Math.floor(rng() * MIX_KINDS.length)];
		if (pick !== undefined) schedule.push(pick);
	}
	const switchIndex = Math.min(
		Math.max(MIX_KINDS.length, Math.floor(count * 0.4)),
		schedule.length - 1
	);
	schedule.splice(switchIndex, 0, 'playlist-switch');
	return schedule;
}

function nowIso(): string {
	return new Date().toISOString();
}

function ignoreUrl(url: string): boolean {
	return (
		url.startsWith('chrome-extension:') ||
		url.startsWith('devtools:') ||
		url.includes('/__playwright')
	);
}

async function query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	return page.evaluate((message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

function pushEvent(session: HuntSession, partial: Omit<HuntEvent, 'ts' | 'action' | 'route'> & { route?: string }): void {
	session.events.push({
		ts: nowIso(),
		action: session.lastAction,
		route: partial.route ?? HUNT_ROUTE,
		...partial
	});
}

function installCollectors(page: Page, session: HuntSession): void {
	page.on('pageerror', (error) => {
		pushEvent(session, { kind: 'pageerror', message: error.stack ?? error.message });
	});
	page.on('console', (message: ConsoleMessage) => {
		if (message.type() !== 'error') return;
		const url = message.location().url;
		if (url !== undefined && ignoreUrl(url)) return;
		pushEvent(session, {
			kind: 'console.error',
			message: message.text(),
			url
		});
	});
	page.on('response', (response: Response) => {
		const status = response.status();
		if (status < 400) return;
		const url = response.url();
		if (ignoreUrl(url)) return;
		const kind: HuntEventKind = status >= 500 ? 'http-5xx' : 'http-4xx';
		pushEvent(session, {
			kind,
			message: `${status} ${response.request().method()} ${url}`,
			url,
			status,
			method: response.request().method(),
			route: (() => {
				try {
					return new URL(url).pathname;
				} catch {
					return url;
				}
			})()
		});
	});
}

async function installPageHooks(page: Page): Promise<void> {
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(
			prefsKey,
			JSON.stringify({ hide_broken_links: false, confirm: { dblclick_load_play: false } })
		);
		const huntWindow = window as unknown as { __huntRejections?: string[] };
		huntWindow.__huntRejections = [];
		window.addEventListener('unhandledrejection', (event) => {
			const bag = window as unknown as { __huntRejections?: string[] };
			(bag.__huntRejections ??= []).push(String(event.reason));
		});
	}, PREFS_STORAGE_KEY);
}

async function drainRejections(page: Page, session: HuntSession): Promise<void> {
	const pending = await page.evaluate(() => {
		const bag = window as unknown as { __huntRejections?: string[] };
		const items = bag.__huntRejections ?? [];
		bag.__huntRejections = [];
		return items;
	});
	for (const message of pending) {
		pushEvent(session, { kind: 'unhandledrejection', message });
	}
}

async function drainToasts(page: Page, session: HuntSession): Promise<void> {
	const ipcToasts = await page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) return [];
		return ipc.toasts().map((toast) => ({ id: toast.id, kind: toast.kind, message: toast.message }));
	});
	const dismissIds: string[] = [];
	for (const toast of ipcToasts) {
		dismissIds.push(toast.id);
		if (toast.kind !== 'error' || session.seenToastIds.has(toast.id)) continue;
		session.seenToastIds.add(toast.id);
		pushEvent(session, { kind: 'toast-error', message: toast.message });
	}
	if (dismissIds.length > 0) {
		await page.evaluate((ids) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return;
			for (const id of ids) ipc.dismissToast(id);
		}, dismissIds);
	}
	const domToasts = await page.locator('.toast-stack .toast.error').evaluateAll((nodes) =>
		nodes.map((node) => ({
			id: node.getAttribute('data-toast-id'),
			message: node.querySelector('.toast-message')?.textContent ?? node.textContent ?? ''
		}))
	);
	for (const toast of domToasts) {
		const id = toast.id ?? toast.message;
		if (id === '' || session.seenToastIds.has(id)) continue;
		session.seenToastIds.add(id);
		pushEvent(session, { kind: 'toast-error', message: toast.message });
	}
}

async function pollFindings(page: Page, session: HuntSession): Promise<void> {
	const state = await query(page);
	const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
	const pressed = (await autoPlay.getAttribute('aria-pressed')) === 'true';
	const now = Date.now();
	const bothStopped = !state.decks[1].playing && !state.decks[2].playing;
	if (pressed && bothStopped) {
		if (session.stall.idleSince === null) session.stall.idleSince = now;
		else if (!session.stall.idleRecorded && now - session.stall.idleSince > STALL_MS) {
			pushEvent(session, { kind: 'stall', message: 'autoplay-idle' });
			session.stall.idleRecorded = true;
		}
	} else {
		session.stall.idleSince = null;
		session.stall.idleRecorded = false;
	}

	const bannerVisible = await page.locator('[data-testid="autoplay-stall-banner"]').isVisible();
	if (bannerVisible && !session.stall.bannerRecorded) {
		pushEvent(session, { kind: 'stall', message: 'autoplay-banner' });
		session.stall.bannerRecorded = true;
	} else if (!bannerVisible) {
		session.stall.bannerRecorded = false;
	}

	for (const deckId of [1, 2] as const) {
		const deck = state.decks[deckId];
		if (deck.playing) {
			if (session.stall.lastPos[deckId] !== null && deck.position_ms === session.stall.lastPos[deckId]) {
				if (session.stall.frozenSince[deckId] === null) session.stall.frozenSince[deckId] = now;
				else if (
					!session.stall.frozenRecorded[deckId] &&
					now - (session.stall.frozenSince[deckId] ?? now) > STALL_MS
				) {
					pushEvent(session, { kind: 'silence', message: `deck-${deckId}-frozen` });
					session.stall.frozenRecorded[deckId] = true;
				}
			} else {
				session.stall.frozenSince[deckId] = null;
				session.stall.frozenRecorded[deckId] = false;
			}
			session.stall.lastPos[deckId] = deck.position_ms;
		} else {
			session.stall.frozenSince[deckId] = null;
			session.stall.frozenRecorded[deckId] = false;
			session.stall.lastPos[deckId] = null;
		}
	}
}

async function collectTick(page: Page, session: HuntSession): Promise<void> {
	await drainRejections(page, session);
	await drainToasts(page, session);
	await pollFindings(page, session);
}

function newSession(): HuntSession {
	return {
		events: [],
		lastAction: 'boot',
		seenToastIds: new Set(),
		stall: {
			idleSince: null,
			idleRecorded: false,
			bannerRecorded: false,
			frozenSince: { 1: null, 2: null },
			frozenRecorded: { 1: false, 2: false },
			lastPos: { 1: null, 2: null }
		}
	};
}

async function openPlaylist(page: Page, name: string): Promise<void> {
	const row = page.getByText(name, { exact: true });
	if (!(await row.isVisible())) {
		await page.locator('.row.folder').filter({ hasText: 'Playlists' }).click();
	}
	await row.click({ force: true });
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

async function ensureAutoPlay(page: Page): Promise<void> {
	const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
	await expect(autoPlay).toBeVisible();
	for (let attempt = 0; attempt < 3 && (await autoPlay.getAttribute('aria-pressed')) !== 'true'; attempt += 1) {
		await autoPlay.click();
	}
	await expect(autoPlay).toHaveAttribute('aria-pressed', 'true');
}

async function loadOntoDeck(page: Page, index: number, deck: 1 | 2): Promise<string> {
	const row = page.locator(TRACK_ROW).nth(index);
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error(`row ${index} has no data-stable-id`);
	const title = row.locator('td.c-title');
	await title.click({ force: true, timeout: 5_000 });
	await expect(row, 'the row did not select, so the quick-load box stays hidden').toHaveClass(
		/rb-row-selected/,
		{ timeout: 3_000 }
	);
	await title.hover();
	await page.waitForTimeout(PAST_GUARD_MS);
	await row.locator(`button[title="Load onto deck ${deck}"]`).click({ force: true, timeout: 5_000 });
	return stableId;
}

async function gotoPerformance(page: Page, session: HuntSession): Promise<void> {
	await installPageHooks(page);
	installCollectors(page, session);
	await page.goto('/performance');
	await expect(page.locator('.rb-topbar').first()).toBeVisible({ timeout: 90_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 90_000
	});
}

test('NEGATIVE CONTROL: a synthetic throw is captured and named', async ({ page }) => {
	test.setTimeout(60_000);
	const session = newSession();
	session.lastAction = 'negative-control';
	await gotoPerformance(page, session);

	await page
		.evaluate(() => {
			throw new Error('autoplay-error-hunt negative control');
		})
		.catch(() => undefined);
	await page
		.evaluate(() => {
			queueMicrotask(() => {
				throw new Error('autoplay-error-hunt negative control');
			});
			void Promise.reject(new Error('autoplay-error-hunt negative control rejection'));
		})
		.catch(() => undefined);
	await expect
		.poll(async () => {
			await drainRejections(page, session);
			return session.events.some((event) => event.message.includes(NEGATIVE_CONTROL_NEEDLE));
		}, { timeout: 5_000 })
		.toBe(true);

	const named = session.events.filter((event) => event.message.includes(NEGATIVE_CONTROL_NEEDLE));
	expect(
		named.map((event) => `${event.kind}:${event.message}`),
		'the probe did not capture the injected throw by name'
	).not.toEqual([]);
	expect(
		named.some((event) => event.message.includes('negative control rejection')),
		'the unhandledrejection capture path did not see the injected rejection'
	).toBe(true);

	const allowlist = loadAllowlist(ALLOW_PATH);
	const unexpected = unexpectedSignatures(groupBySignature(session.events), allowlist);
	expect(unexpected.length, 'unexpectedSignatures must name the injected throw').toBeGreaterThan(0);
	expect(unexpected.some((finding) => finding.signature.includes(NEGATIVE_CONTROL_NEEDLE))).toBe(true);
});

test(`AutoPlay mixing hunt reports every error over ${huntMinutes()} minutes`, async ({ page }) => {
	const minutes = huntMinutes();
	const seed = huntSeed();
	const durationMs = HUNT_DURATION_MS;
	const session = newSession();
	const hunt = autoplayHuntFixture(FIXTURE_MANIFEST_PATH);
	const rng = mulberry32(seed);
	const schedule = buildSchedule(seed, durationMs);
	const actionsRan: string[] = [];
	let eqBandIndex = 0;
	let playlistSwitched = false;

	await gotoPerformance(page, session);
	await dispatch(page, { type: 'master_volume', value: MASTER_VOLUME });
	await openPlaylist(page, hunt.playlistA.name);

	const visibleRows = await page.locator(TRACK_ROW).count();
	if (visibleRows < 6) {
		throw new Error(
			`UNKNOWN: fixture library has ${visibleRows} visible track row(s) after boot, need >= 6`
		);
	}

	await ensureAutoPlay(page);
	const loadedId = await loadOntoDeck(page, 0, 1);
	await expect
		.poll(async () => (await query(page)).decks[1].stable_id, { timeout: 45_000 })
		.toBe(loadedId);
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await expect
		.poll(async () => {
			const deck = (await query(page)).decks[1];
			return deck.playing && deck.stable_id !== null;
		})
		.toBe(true);
	if (!(await query(page)).decks[1].is_master) {
		await dispatch(page, { type: 'master', deck: 1 });
	}

	const startedAt = nowIso();
	const startMs = Date.now();
	const endMs = startMs + durationMs;
	const overrunDeadline = startMs + durationMs + 120_000;
	let nextActionAt = startMs;
	let actionIndex = 0;

	const requiredLeft = (): string[] => REQUIRED_KINDS.filter((kind) => !actionsRan.includes(kind));

	const runAction = async (kind: ActionKind): Promise<void> => {
		session.lastAction = kind;
		const state = await query(page);
		const master = state.master_deck === 2 ? 2 : 1;
		const otherDeck: 1 | 2 = master === 1 ? 2 : 1;
		const arrow = rng() < 0.5 ? 'ArrowDown' : 'ArrowUp';
		const xfArrow = rng() < 0.5 ? 'ArrowLeft' : 'ArrowRight';
		const deck: 1 | 2 = rng() < 0.5 ? 1 : 2;

		if (kind === 'load-other-deck') {
			const currentId = state.decks[otherDeck].stable_id;
			const rows = page.locator(TRACK_ROW);
			const count = await rows.count();
			let index = Math.floor(rng() * count);
			for (let probe = 0; probe < count; probe += 1) {
				const candidate = (index + probe) % count;
				const id = await rows.nth(candidate).getAttribute('data-stable-id');
				if (id !== null && id !== currentId) {
					index = candidate;
					break;
				}
			}
			await loadOntoDeck(page, index, otherDeck);
		} else if (kind === 'crossfader') {
			const slider = page.getByRole('slider', { name: 'crossfader' });
			await slider.focus();
			await slider.press(xfArrow);
		} else if (kind === 'eq') {
			const band = EQ_BANDS[eqBandIndex % EQ_BANDS.length];
			eqBandIndex += 1;
			const knob = page.getByTestId(`knob-${deck}:${band}`);
			await knob.focus();
			await knob.press(arrow);
		} else if (kind === 'filter') {
			const knob = page.getByTestId(`knob-${deck}:filter`);
			await knob.focus();
			await knob.press(arrow);
		} else if (kind === 'tempo') {
			const fader = page.getByTestId(`pitch-fader-deck-${deck}`);
			await fader.focus();
			await fader.press(arrow);
		} else if (kind === 'sync') {
			const button = page.getByTestId(`beat-sync-deck-${deck}`);
			if (await button.isDisabled()) {
				session.lastAction = 'sync:inert-gridless';
				await button.click({ force: true });
			} else {
				await button.click();
			}
		} else if (kind === 'playlist-switch') {
			if (playlistSwitched) return;
			await openPlaylist(page, hunt.playlistB.name);
			await expect(page.getByRole('button', { name: 'AutoPlay', exact: true })).toHaveAttribute(
				'aria-pressed',
				'true'
			);
			playlistSwitched = true;
		}

		if (!actionsRan.includes(kind)) actionsRan.push(kind);
	};

	const persist = (unknownReason: string | null) => {
		const allowlist = loadAllowlist(ALLOW_PATH);
		const report = buildHuntReport({
			minutesRequested: minutes,
			seed,
			startedAt,
			endedAt: nowIso(),
			unknownReason,
			events: session.events,
			allowlist,
			actionsRan
		});
		writeHuntReport(report, [HUNT_HTML_REPORT_DIR, HUNT_OUTPUT_DIR]);
		return report;
	};

	try {
		while (Date.now() < endMs || requiredLeft().length > 0) {
			if (Date.now() > overrunDeadline) break;
			await collectTick(page, session);
			if (actionIndex < schedule.length && Date.now() >= nextActionAt) {
				const kind = schedule[actionIndex];
				actionIndex += 1;
				if (kind !== undefined && (kind !== 'playlist-switch' || !playlistSwitched)) {
					try {
						await runAction(kind);
					} catch (error) {
						const message = error instanceof Error ? error.message : String(error);
						console.log(`scripted ${kind} skipped: ${message}`);
						if (!actionsRan.includes(kind)) actionsRan.push(kind);
					}
				}
				nextActionAt = Date.now() + ACTION_INTERVAL_MS;
			}
			await page.waitForTimeout(500);
		}
		await collectTick(page, session);

		const missing = requiredLeft();
		expect(
			missing,
			`scripted actions never ran, so the hunt did not mix. Ran: [${actionsRan.join(', ')}]`
		).toEqual([]);

		const report = persist(null);
		const summary = formatReport(report.findings);
		console.log(summary);
		expect(report.unexpected, summary).toEqual([]);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		persist(message.startsWith('UNKNOWN:') ? message : null);
		throw error;
	}
});
