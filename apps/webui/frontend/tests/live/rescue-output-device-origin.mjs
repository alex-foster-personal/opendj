/**
 * RESCUE-05 end to end on real hardware: pick an output in the app on ONE
 * origin, let the app's own ring writer capture it, then boot the app on
 * ANOTHER origin and check that its own Crash Rescue auto-restore routes the
 * master to the same physical output.
 *
 * Why live: the bug only exists where the browser salts device ids per origin
 * and the page origin changes per launch (the desktop shell's fresh loopback
 * port). Nothing here is simulated or hand-built. The pick goes through the
 * app's IPC, the snapshot is built and posted by the production ring writer to
 * the real engine ring, and the restore is the page's boot-time
 * runPerformanceRescueAutoRestore reading that ring back. The ids and labels
 * come from the real outputs.
 *
 * The two origins are one dev server reached as `localhost` and as
 * `127.0.0.1`: different origins to the browser, so they get different salted
 * ids, the same way two launches on two ports do.
 *
 * Run (from apps/webui/frontend, with the engine and `pnpm dev` up, and a
 * FRESH engine data dir so no older snapshot is newer than the one this makes):
 *
 *   node tests/live/rescue-output-device-origin.mjs --url http://127.0.0.1:<frontend port>
 *   node tests/live/rescue-output-device-origin.mjs --url ... --channel msedge
 *
 * Prints JSON. Exit 0 PASS, 1 FAIL, 2 UNAVAILABLE (no suitable real output, or
 * this engine does not salt per origin, so the case cannot be shown here).
 *
 * Regression lines:
 *  - if picking a sink while nothing plays writes no ring snapshot, a crash
 *    after setting up outputs restores none of them
 *  - if the boot restore on the second origin does not route the master to the
 *    same labeled output under that origin's id, RESCUE-05 is broken
 *  - if that boot throws "is not an enumerated headphone output", the bifrost1
 *    boot abort is back
 *  - NEGATIVE CONTROL: if the first origin's id handed straight to the selector
 *    on the second origin is NOT refused, this browser did not reproduce the
 *    defect and the lines above prove nothing
 */
import { chromium } from '@playwright/test';

function arg(name, fallback = null) {
	const i = process.argv.indexOf(`--${name}`);
	return i === -1 ? fallback : (process.argv[i + 1] ?? fallback);
}
const APP_URL = arg('url');
const CHANNEL = arg('channel', 'chromium');
if (APP_URL === null) throw new Error('--url <frontend dev server origin on 127.0.0.1> is required');
const ORIGIN_B = new URL(APP_URL).origin;
const ORIGIN_A = ORIGIN_B.replace('127.0.0.1', 'localhost');
if (ORIGIN_A === ORIGIN_B) throw new Error('--url must use 127.0.0.1 so localhost can serve as the other origin');
const EXIT_CODE = { PASS: 0, FAIL: 1, UNAVAILABLE: 2 };
const RESTORE_WAIT_MS = 30_000;
/** Labels the enumeration policy would auto-pin as master on its own
 * (outputLooksLikeSpeakers). Picking one of those could pass without the
 * restore doing anything, so the probe never picks one. */
const AUTO_PIN_LABEL = /speaker|built-in output|macbook/i;

async function openPerformance(context, origin, pageErrors) {
	const page = await context.newPage();
	page.on('pageerror', (error) => pageErrors.push(`${origin}: ${String(error)}`));
	await page.goto(`${origin}/performance`);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, null, { timeout: 60_000 });
	return page;
}

/** captured_at_ms of the newest ring snapshot (0 when the ring is empty). Not a
 * count: the ring is capped at 8 slots, so a count stops moving once it is full. */
const newestSnapshotAt = (page) =>
	page.evaluate(async () => {
		const response = await fetch('/api/v1/rescue/snapshots');
		if (!response.ok) throw new Error(`GET /api/v1/rescue/snapshots: HTTP ${response.status}`);
		return (await response.json()).snapshots[0]?.captured_at_ms ?? 0;
	});

/** The whole measurement; returns a verdict instead of exiting, so the browser
 * is always closed first. */
async function probe(browser) {
	const context = await browser.newContext();
	for (const origin of [ORIGIN_A, ORIGIN_B]) await context.grantPermissions(['microphone'], { origin });
	const pageErrors = [];

	// Launch 1, origin A: pick an output the app would not pick by itself.
	const pageA = await openPerformance(context, ORIGIN_A, pageErrors);
	await pageA.evaluate(async () => {
		const prefs = await import('/src/lib/rb/prefs.svelte.ts');
		prefs.setAppPosture('gig');
		await window.musicDjToolsPerformance.dispatch({ type: 'headphone_outputs_refresh' });
	});
	const stateA = await pageA.evaluate(() => window.musicDjToolsPerformance.query().mixer.headphones);
	const labelCounts = new Map();
	for (const output of stateA.outputs) labelCounts.set(output.label, (labelCounts.get(output.label) ?? 0) + 1);
	const chosen = stateA.outputs.find(
		(output) =>
			!['default', 'communications'].includes(output.id) &&
			output.label !== '' &&
			labelCounts.get(output.label) === 1 &&
			!AUTO_PIN_LABEL.test(output.label) &&
			output.id !== stateA.selected_master_output_device_id
	);
	if (chosen === undefined) {
		return {
			verdict: 'UNAVAILABLE',
			reason: 'no real output with a unique visible label that the auto-pin policy would not choose anyway',
			outputs: stateA.outputs.map((output) => output.label)
		};
	}
	const ringBefore = await newestSnapshotAt(pageA);
	await pageA.evaluate(
		(id) => window.musicDjToolsPerformance.dispatch({ type: 'headphone_master_select', device_id: id }),
		chosen.id
	);
	await pageA.waitForTimeout(1_000);
	const ringAfter = await newestSnapshotAt(pageA);
	await pageA.close();

	// Launch 2, origin B: the page's own boot restore reads the ring back.
	const pageB = await openPerformance(context, ORIGIN_B, pageErrors);
	await pageB.evaluate(() => window.musicDjToolsPerformance.dispatch({ type: 'headphone_outputs_refresh' }));
	const outputsB = await pageB.evaluate(() => window.musicDjToolsPerformance.query().mixer.headphones.outputs);
	const sameDeviceB = outputsB.find((output) => output.label === chosen.label);
	if (sameDeviceB === undefined) {
		return { verdict: 'FAIL', reason: `'${chosen.label}' is not enumerated under ${ORIGIN_B}` };
	} else if (sameDeviceB.id === chosen.id) {
		return { verdict: 'UNAVAILABLE', reason: 'this engine did not salt the id per origin; the case cannot be shown' };
	}
	let masterB = null;
	const deadline = Date.now() + RESTORE_WAIT_MS;
	while (Date.now() < deadline) {
		masterB = await pageB.evaluate(
			() => window.musicDjToolsPerformance.query().mixer.headphones.selected_master_output_device_id
		);
		if (masterB === sameDeviceB.id) break;
		await pageB.waitForTimeout(250);
	}

	// NEGATIVE CONTROL: the pre-fix replay, origin A's id straight into the selector.
	const control = await pageB.evaluate(async (foreignId) => {
		try {
			await window.musicDjToolsPerformance.dispatch({ type: 'headphone_master_select', device_id: foreignId });
			return null;
		} catch (error) {
			return String(error);
		}
	}, chosen.id);

	const checks = {
		pick_wrote_ring_snapshot: ringAfter > ringBefore,
		boot_restore_routed_master_to_same_output: masterB === sameDeviceB.id,
		no_uncaught_enumeration_error: !pageErrors.some((error) => /enumerated headphone output/.test(error)),
		control_refused_foreign_id: control !== null && /not an enumerated headphone output/.test(control)
	};
	return {
		verdict: Object.values(checks).every(Boolean) ? 'PASS' : 'FAIL',
		chosen_label: chosen.label,
		id_origin_a: chosen.id.slice(0, 12),
		id_origin_b: sameDeviceB.id.slice(0, 12),
		master_after_boot_b: masterB === null ? null : masterB.slice(0, 12),
		ring_newest_captured_at_ms: { before: ringBefore, after: ringAfter },
		control,
		page_errors: pageErrors,
		checks
	};
}

const browser = await chromium.launch({ channel: CHANNEL === 'chromium' ? undefined : CHANNEL });
let outcome;
try {
	outcome = await probe(browser);
} finally {
	await browser.close();
}
console.log(JSON.stringify({ channel: CHANNEL, origins: [ORIGIN_A, ORIGIN_B], ...outcome }, null, 2));
process.exitCode = EXIT_CODE[outcome.verdict];
