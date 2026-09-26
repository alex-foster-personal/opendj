/**
 * RESCUE-05 on real hardware: does a Crash Rescue sink saved under ONE origin
 * restore under ANOTHER, through the shipped restore path, in a real browser?
 *
 * Why live: the bug only exists where the browser salts device ids per origin
 * and the page origin changes per launch (the desktop shell's fresh loopback
 * port). Unit tests can replay the resolution logic; only a real browser with
 * real outputs can show an id captured on origin A failing on origin B and the
 * label bringing it back. Nothing here is simulated: the ids, labels, IPC
 * dispatcher, headphones.ts enumeration and auto-pin policy, and setSinkId are
 * the real ones, imported from the running dev server's module graph.
 *
 * Run (from apps/webui/frontend, with the dev server up: `pnpm dev`):
 *
 *   node tests/live/rescue-output-device-origin.mjs --url http://127.0.0.1:<frontend port>
 *   node tests/live/rescue-output-device-origin.mjs --url ... --channel msedge
 *
 * Origin A is a blank page this probe serves on another loopback port, standing
 * in for the crashed launch. Origin B is the app. Prints JSON; exit 0 PASS,
 * 1 FAIL, 2 UNAVAILABLE (no real labeled output, or this engine does not salt,
 * so the cross-origin case cannot be shown here).
 *
 * Regression lines:
 *  - if the NEGATIVE CONTROL (the foreign id handed straight to the selector)
 *    does not refuse, then this browser did not reproduce the bifrost1 defect and
 *    every other line proves nothing
 *  - if an id saved under origin A with its label is not restored under origin B
 *    as `restored_by_label` onto origin B's id for that label, RESCUE-05 is broken
 *  - if a saved output that is absent, or a legacy id-only snapshot, makes the
 *    restore throw, the bifrost1 boot abort is back
 */
import { createServer } from 'node:http';

import { chromium } from '@playwright/test';

function arg(name, fallback = null) {
	const i = process.argv.indexOf(`--${name}`);
	return i === -1 ? fallback : (process.argv[i + 1] ?? fallback);
}
const APP_URL = arg('url');
const CHANNEL = arg('channel', 'chromium');
if (APP_URL === null) throw new Error('--url <frontend dev server origin> is required');
const PSEUDO_DEVICE_IDS = new Set(['default', 'communications']);

const EXIT_CODE = { PASS: 0, FAIL: 1, UNAVAILABLE: 2 };

function serveBlankOrigin() {
	return new Promise((resolve) => {
		const server = createServer((_request, response) => {
			response.writeHead(200, { 'content-type': 'text/html' });
			response.end('<!doctype html><title>crashed-launch origin</title>');
		});
		server.listen(0, '127.0.0.1', () => resolve(server));
	});
}

const listOutputs = () =>
	navigator.mediaDevices
		.enumerateDevices()
		.then((devices) =>
			devices
				.filter((device) => device.kind === 'audiooutput')
				.map((device) => ({ id: device.deviceId, label: device.label }))
		);

/** One restore of a sink-only snapshot through the SHIPPED executeRescueRestore,
 * with its real defaults (IPC dispatcher, mirror query, toast store). Empty decks
 * and play mode, so nothing but the mixer and sink steps runs. */
async function restoreSinkOnly(page, master) {
	return page.evaluate(async (savedMaster) => {
		const restore = await import('/src/lib/rb/rescue-restore.svelte.ts');
		const snapshots = await import('/src/lib/rb/rescue-snapshot.ts');
		const emptyDeck = (deck_id) => ({
			deck_id,
			stable_id: null,
			source_path: null,
			playing: false,
			position_ms: 0,
			beat_stamp: { kind: 'sample', position_ms: 0 },
			pitch: 1,
			pitch_range: 8,
			master_tempo_enabled: true,
			key_sync_enabled: false,
			quantize_enabled: true,
			beat_sync_enabled: true,
			sync_mode: 'bar',
			is_master: false,
			cue_ms: null,
			loop: null,
			hot_cue_armed: null,
			stems: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			},
			mixer_channel: {
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				filter: 0.5,
				fader: 1,
				assign: 'THRU',
				cue_enabled: false
			}
		});
		const ipc = window.musicDjToolsPerformance;
		const headphones = {
			mix: 0,
			level: 0.5,
			output_mode: ipc.query().mixer.headphones.output_mode,
			selected_output_device_id: null,
			selected_master_output_device_id: savedMaster.device_id
		};
		if (savedMaster.label !== undefined) headphones.selected_master_output_device_label = savedMaster.label;
		const snapshot = snapshots.parseRescueSnapshot({
			schema: 1,
			captured_at_ms: Date.now(),
			reason: 'transport',
			app_posture: 'gig',
			master_deck: null,
			playlist_id: null,
			decks: { 1: emptyDeck(1), 2: emptyDeck(2), 3: emptyDeck(3), 4: emptyDeck(4) },
			mixer: { crossfader: 0.5, master: ipc.query().mixer.master, headphones }
		});
		try {
			const result = await restore.executeRescueRestore({ snapshot, mode: 'play' });
			return {
				threw: null,
				sink: result.sinks.master,
				mirror_master: ipc.query().mixer.headphones.selected_master_output_device_id
			};
		} catch (error) {
			return { threw: String(error), sink: null, mirror_master: null };
		}
	}, master);
}

/** The whole measurement; returns a verdict instead of exiting, so the browser
 * and the foreign-origin server are always closed first. */
async function probe(browser, originA) {
	const context = await browser.newContext();
	await context.grantPermissions(['microphone'], { origin: originA });
	await context.grantPermissions(['microphone'], { origin: new URL(APP_URL).origin });

	const pageA = await context.newPage();
	await pageA.goto(`${originA}/`);
	const outputsA = await pageA.evaluate(listOutputs);
	await pageA.close();
	const labelCounts = new Map();
	for (const output of outputsA) labelCounts.set(output.label, (labelCounts.get(output.label) ?? 0) + 1);
	const chosen = outputsA.find(
		(output) => !PSEUDO_DEVICE_IDS.has(output.id) && output.label !== '' && labelCounts.get(output.label) === 1
	);
	if (chosen === undefined) {
		return { verdict: 'UNAVAILABLE', reason: 'no real audio output with a unique visible label', outputs_a: outputsA.length };
	}

	const pageB = await context.newPage();
	const pageErrors = [];
	pageB.on('pageerror', (error) => pageErrors.push(String(error)));
	await pageB.goto(`${APP_URL}/performance`);
	await pageB.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, null, { timeout: 60_000 });
	await pageB.evaluate(() => window.musicDjToolsPerformance.dispatch({ type: 'headphone_outputs_refresh' }));
	const outputsB = await pageB.evaluate(() => window.musicDjToolsPerformance.query().mixer.headphones.outputs);
	const sameDeviceB = outputsB.find((output) => output.label === chosen.label);
	if (sameDeviceB === undefined) {
		return { verdict: 'FAIL', reason: `label '${chosen.label}' missing under the app origin` };
	} else if (sameDeviceB.id === chosen.id) {
		return { verdict: 'UNAVAILABLE', reason: 'this engine did not salt the id per origin; the cross-origin case cannot be shown' };
	}

	// NEGATIVE CONTROL: the pre-fix replay, the foreign id straight into the selector.
	const control = await pageB.evaluate(async (foreignId) => {
		try {
			await window.musicDjToolsPerformance.dispatch({ type: 'headphone_master_select', device_id: foreignId });
			return null;
		} catch (error) {
			return String(error);
		}
	}, chosen.id);

	const byLabel = await restoreSinkOnly(pageB, { device_id: chosen.id, label: chosen.label });
	const absent = await restoreSinkOnly(pageB, {
		device_id: chosen.id,
		label: 'RESCUE-05 live probe: an output that is not plugged in'
	});
	const legacy = await restoreSinkOnly(pageB, { device_id: chosen.id });

	const checks = {
		control_refused_foreign_id: control !== null && /not an enumerated headphone output/.test(control),
		by_label_restored_onto_origin_b_id:
			byLabel.threw === null &&
			byLabel.sink.outcome === 'restored_by_label' &&
			byLabel.sink.device_id === sameDeviceB.id &&
			byLabel.mirror_master === sameDeviceB.id,
		absent_reported_not_thrown: absent.threw === null && absent.sink.outcome === 'not_found',
		absent_names_output_in_effect: absent.threw === null && absent.sink.device_id === absent.mirror_master,
		legacy_id_only_reported_not_thrown: legacy.threw === null && legacy.sink.outcome === 'not_found',
		no_uncaught_enumeration_error: !pageErrors.some((error) => /enumerated headphone output/.test(error))
	};
	return {
		verdict: Object.values(checks).every(Boolean) ? 'PASS' : 'FAIL',
		chosen_label: chosen.label,
		id_origin_a: chosen.id.slice(0, 12),
		id_origin_b: sameDeviceB.id.slice(0, 12),
		control,
		by_label: byLabel,
		absent,
		legacy,
		page_errors: pageErrors,
		checks
	};
}

const foreignOrigin = await serveBlankOrigin();
const originA = `http://127.0.0.1:${foreignOrigin.address().port}`;
const browser = await chromium.launch({ channel: CHANNEL === 'chromium' ? undefined : CHANNEL });
let outcome;
try {
	outcome = await probe(browser, originA);
} finally {
	foreignOrigin.close();
	await browser.close();
}
console.log(JSON.stringify({ channel: CHANNEL, app: APP_URL, ...outcome }, null, 2));
process.exitCode = EXIT_CODE[outcome.verdict];
