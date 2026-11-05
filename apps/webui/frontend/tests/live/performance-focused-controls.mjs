/**
 * Issue5761: actual focused controls and real decoded transport on /performance.
 * Attach only to an explicitly supplied isolated runtime/data copy. No fixture
 * generation, injected controls, audio replacements or application-state edits.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { chromium } from 'playwright';

const { values } = parseArgs({
	options: {
		'base-url': { type: 'string' },
		'track-id': { type: 'string' },
		out: { type: 'string' },
		head: { type: 'string' },
		'browser-channel': { type: 'string' }
	}
});
for (const name of ['base-url', 'track-id', 'out', 'head']) {
	assert.ok(values[name], `--${name} is required; unavailable inputs cannot pass acceptance`);
}
const origin = new URL(values['base-url']);
assert.equal(origin.protocol, 'http:');
assert.equal(
	origin.hostname,
	'127.0.0.1',
	'only an explicitly isolated loopback runtime is permitted'
);
assert.ok(origin.port);
assert.ok(process.env.AF_SERVICE_ID, 'owned browser processes require AF_SERVICE_ID');
const source = readFileSync(
	new URL('../../src/lib/rb/performance-shortcut-routing.ts', import.meta.url)
);
const report = {
	observed_at: new Date().toISOString(),
	head: values.head,
	base_url: origin.origin,
	track_id: values['track-id'],
	routing_sha256: createHash('sha256').update(source).digest('hex'),
	checks: [],
	safety: [],
	key_events: [],
	console_errors: [],
	page_errors: []
};
const browser = await chromium.launch({
	headless: true,
	...(values['browser-channel'] ? { channel: values['browser-channel'] } : {}),
	args: ['--autoplay-policy=no-user-gesture-required']
});
report.browser_version = browser.version();
report.browser_channel = values['browser-channel'] ?? 'playwright-chromium';
const page = await browser.newPage();
page.on('console', (message) => {
	if (message.type() === 'error')
		report.console_errors.push({ observed_at: new Date().toISOString(), text: message.text() });
});
page.on('pageerror', (error) =>
	report.page_errors.push({ observed_at: new Date().toISOString(), text: String(error) })
);
const save = () => writeFileSync(values.out, JSON.stringify(report, null, 2) + '\n');
async function query() {
	return page.evaluate(() => window.musicDjToolsPerformance.query());
}
async function dispatch(command) {
	return page.evaluate((command) => window.musicDjToolsPerformance.dispatch(command), command);
}
function safe(state) {
	assert.equal(state.mixer.master, 0);
	assert.equal(state.mixer.headphones.level, 0);
	assert.equal(state.master.muted, true);
	for (const channel of Object.values(state.mixer.channels))
		assert.equal(channel.cue_enabled, false);
}
async function settleMuted(label) {
	for (const command of [
		{ type: 'master_volume', value: 0 },
		{ type: 'headphone_level', value: 0 },
		{ type: 'master_mute', muted: true, persist: false },
		...[1, 2, 3, 4].map((deck) => ({ type: 'channel_cue', deck, enabled: false }))
	])
		await dispatch(command);
	for (let i = 0; i < 10; i++) {
		const state = await query();
		safe(state);
		report.safety.push({ label, observed_at: new Date().toISOString(), state });
		await page.waitForTimeout(300);
	}
}
async function settled(playing) {
	await page.waitForFunction(
		(playing) => {
			const state = window.musicDjToolsPerformance.query(),
				d = state.decks[1];
			return (
				d.playing === playing &&
				d.audible === playing &&
				!d.transport_pending &&
				d.transport_clock.desired_revision === d.transport_clock.presented_revision &&
				!state.command_pending &&
				state.command_queued === 0
			);
		},
		playing,
		{ timeout: 15000 }
	);
	const state = await query();
	safe(state);
	return state;
}
async function ensurePlaying() {
	await dispatch({ type: 'play', deck: 1, playing: false });
	await settled(false);
	await dispatch({ type: 'seek', deck: 1, position_ms: 131 });
	safe(await query());
	await dispatch({ type: 'play', deck: 1, playing: true, quantize: false });
	return settled(true);
}
function unchangedTransport(before, after) {
	safe(before);
	safe(after);
	for (const key of ['stable_id', 'playing', 'audible', 'pitch', 'load_generation']) {
		assert.equal(after.decks[1][key], before.decks[1][key], key);
	}
	assert.equal(after.decks[1].transport_pending, false);
	assert.equal(after.decks[1].transport_clock.source, 'audio_output');
	for (const key of ['desired_revision', 'presented_revision']) {
		assert.equal(after.decks[1].transport_clock[key], before.decks[1].transport_clock[key], key);
	}
	assert.ok(
		after.decks[1].position_ms > before.decks[1].position_ms,
		'presented playback must keep advancing'
	);
}
async function bodyFocus() {
	await page.evaluate(
		() => document.activeElement instanceof HTMLElement && document.activeElement.blur()
	);
	assert.equal(await page.evaluate(() => document.activeElement === document.body), true);
}
async function closeJobsIfOpen() {
	const panel = page.locator('.jobs-panel');
	if (await panel.isVisible())
		await panel.getByRole('button', { name: 'Close', exact: true }).click();
}
async function check(name, action) {
	const row = { name, observed_at: new Date().toISOString() };
	try {
		await action(row);
		row.passed = true;
	} catch (error) {
		row.passed = false;
		row.error = String(error);
	}
	report.checks.push(row);
	save();
	console.log(JSON.stringify({ name, passed: row.passed, error: row.error }));
}

try {
	await page.goto(origin.origin + '/performance', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60000
	});
	for (const command of [
		{ type: 'master_volume', value: 0 },
		{ type: 'headphone_level', value: 0 },
		{ type: 'master_mute', muted: true, persist: false }
	])
		await dispatch(command);
	await page.waitForTimeout(8000); // Real preference hydration must finish before transport.
	await settleMuted('initial-after-prefs');
	const autoPlay = page.getByRole('button', { name: 'AutoPlay', exact: true });
	if ((await autoPlay.getAttribute('aria-pressed')) === 'true') await autoPlay.click();
	assert.equal(await autoPlay.getAttribute('aria-pressed'), 'false');
	await dispatch({ type: 'load', deck: 1, stable_id: values['track-id'] });
	await dispatch({ type: 'beat_sync', deck: 1, enabled: false });
	await dispatch({ type: 'master', deck: 1, lock: true });
	await settleMuted('loaded-before-transport');
	report.loaded = await query();
	assert.equal(report.loaded.decks[1].stable_id, values['track-id']);
	assert.ok(report.loaded.decks[1].duration_ms > 30000);
	assert.equal(report.loaded.decks[1].processor_error, null);
	await page.evaluate(() => {
		window.__focusedControlKeyEvents = [];
		window.addEventListener(
			'keydown',
			(event) => {
				if (!['Space', 'Tab'].includes(event.code)) return;
				window.__focusedControlKeyEvents.push({
					observed_at: new Date().toISOString(),
					code: event.code,
					defaultPrevented: event.defaultPrevented,
					trusted: event.isTrusted,
					tag: event.target instanceof Element ? event.target.tagName : null,
					role: event.target instanceof Element ? event.target.getAttribute('role') : null
				});
			},
			{ capture: true }
		);
	});
	await check(
		'Space activates actual AutoPlay checkbox and preserves playing transport',
		async (row) => {
			await ensurePlaying();
			await page.waitForTimeout(300);
			report.playing_capture = await page.evaluate(() => window.musicDjToolsPerformance.capture(1));
			const wave = report.playing_capture.time_domain;
			report.capture_rms = Math.sqrt(wave.reduce((sum, x) => sum + x * x, 0) / wave.length);
			assert.ok(
				Number.isFinite(report.capture_rms) && report.capture_rms > 0,
				'real DSP capture must contain decoded signal'
			);
			await autoPlay.hover();
			const checkbox = page.getByRole('checkbox', { name: 'Enforce play order', exact: true });
			await checkbox.waitFor({ state: 'visible' });
			await checkbox.hover();
			await checkbox.focus();
			row.focused = await checkbox.evaluate((el) => document.activeElement === el);
			assert.equal(row.focused, true);
			row.checked_before = await checkbox.isChecked();
			row.before = await query();
			await page.keyboard.press('Space');
			await page.waitForTimeout(500);
			row.checked_after = await checkbox.isChecked();
			row.after = await query();
			assert.equal(row.checked_after, !row.checked_before, 'focused native checkbox must toggle');
			unchangedTransport(row.before, row.after);
		}
	);
	await check('Space activates actual Jobs button and preserves playing transport', async (row) => {
		await ensurePlaying();
		const button = page.getByRole('button', { name: 'Jobs drawer', exact: true });
		assert.equal(await button.isEnabled(), true);
		await button.hover();
		await closeJobsIfOpen();
		await button.focus();
		row.focused = await button.evaluate((el) => document.activeElement === el);
		assert.equal(row.focused, true);
		row.expanded_before = await button.getAttribute('aria-expanded');
		row.before = await query();
		await page.keyboard.press('Space');
		await page.waitForTimeout(500);
		row.expanded_after = await button.getAttribute('aria-expanded');
		row.after = await query();
		assert.equal(row.expanded_before, 'false');
		assert.equal(row.expanded_after, 'true', 'focused native button must activate');
		unchangedTransport(row.before, row.after);
		await closeJobsIfOpen();
	});
	await check('Body Space retains actual immediate play/pause', async (row) => {
		await closeJobsIfOpen();
		await ensurePlaying();
		await bodyFocus();
		row.before = await query();
		await page.keyboard.press('Space');
		row.paused = await settled(false);
		assert.equal(row.paused.decks[1].stable_id, values['track-id']);
		assert.ok(
			row.paused.decks[1].transport_clock.desired_revision >
				row.before.decks[1].transport_clock.desired_revision
		);
		await bodyFocus();
		await page.keyboard.press('Space');
		row.resumed = await settled(true);
	});
	const compatible = page.locator('.next-only input[type="checkbox"]');
	for (const target of ['body', 'button', 'checkbox']) {
		await check(
			`Tab moves focus from ${target} without changing compatible filter`,
			async (row) => {
				await ensurePlaying();
				if (target === 'body') await bodyFocus();
				else if (target === 'button')
					await page.getByRole('button', { name: 'Jobs drawer', exact: true }).focus();
				else await compatible.focus();
				row.filter_before = await compatible.isChecked();
				row.focus_before = await page.evaluate(() => ({
					tag: document.activeElement.tagName,
					html: document.activeElement.outerHTML.slice(0, 500)
				}));
				row.before = await query();
				await page.evaluate(() => {
					window.__focusedControlBeforeTab = document.activeElement;
				});
				await page.keyboard.press('Tab');
				await page.waitForTimeout(300);
				row.filter_after = await compatible.isChecked();
				row.after = await query();
				row.focus_changed = await page.evaluate(
					() => document.activeElement !== window.__focusedControlBeforeTab
				);
				row.focus_after = await page.evaluate(() => ({
					tag: document.activeElement.tagName,
					html: document.activeElement.outerHTML.slice(0, 500)
				}));
				assert.equal(row.focus_changed, true, 'Tab must move native focus');
				assert.equal(row.filter_after, row.filter_before, 'Tab must not toggle next-only filter');
				unchangedTransport(row.before, row.after);
			}
		);
	}
	report.key_events = await page.evaluate(() => window.__focusedControlKeyEvents);
	assert.equal(report.page_errors.length, 0);
} finally {
	try {
		if (await page.evaluate(() => Boolean(window.musicDjToolsPerformance)).catch(() => false)) {
			for (const deck of [4, 3, 2, 1]) await dispatch({ type: 'play', deck, playing: false });
			await settleMuted('final-stopped');
			report.final = await query();
			for (const deck of Object.values(report.final.decks)) {
				assert.equal(deck.playing, false);
				assert.equal(deck.audible, false);
				assert.equal(deck.transport_pending, false);
				assert.equal(
					deck.transport_clock.desired_revision,
					deck.transport_clock.presented_revision
				);
			}
			assert.equal(report.final.command_queued, 0);
			assert.equal(report.final.command_pending, false);
		}
	} finally {
		report.completed_at = new Date().toISOString();
		save();
		await browser.close();
	}
}
assert.equal(
	report.checks.filter((check) => !check.passed).length,
	0,
	'real focused-control acceptance failed; retained results identify each failure'
);
