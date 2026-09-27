// Probe: does this browser engine salt MediaDeviceInfo.deviceId per ORIGIN? (RESCUE-05)
//
// Why: the desktop shell serves the page from http://127.0.0.1:<port> with an
// OS-chosen port per launch, so a per-origin salt makes every saved output id
// unresolvable on the next launch. Decision record:
// docs/decisions/ADR-NEW-output-device-descriptor-survives-origin-change.md.
//
// Usage (from apps/webui/frontend):
//   node scripts/probe-device-id-salting.mjs [--channel msedge|chrome|chromium]
// `msedge` is the engine WebView2 embeds. Needs at least one real audio output.
//
// The hypothesis predicts TWO things, and the probe checks both, because either
// alone is ambiguous: (1) the same physical output reports a DIFFERENT id under
// two loopback ports in one profile, and (2) the SAME origin reports the SAME id
// after a browser restart on that profile. Without (2), a salt rotated per launch
// would read exactly like a per-origin salt. It prints UNKNOWN and exits 2 when it
// cannot measure (no outputs, hidden labels), never a verdict.
import { mkdtempSync, rmSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from 'node:util';

import { chromium } from '@playwright/test';

const { values } = parseArgs({ options: { channel: { type: 'string', default: 'msedge' } } });
const PSEUDO_DEVICE_IDS = new Set(['default', 'communications']);

function serveBlankPage() {
	return new Promise((resolve) => {
		const server = http.createServer((_request, response) => {
			response.writeHead(200, { 'content-type': 'text/html' });
			response.end('<!doctype html><title>device-id probe</title>');
		});
		server.listen(0, '127.0.0.1', () => resolve(server));
	});
}

async function enumerateOutputs(context, origin) {
	await context.grantPermissions(['microphone'], { origin });
	const page = await context.newPage();
	await page.goto(`${origin}/`);
	const outputs = await page.evaluate(async () =>
		(await navigator.mediaDevices.enumerateDevices())
			.filter((device) => device.kind === 'audiooutput')
			.map((device) => ({ id: device.deviceId, group_id: device.groupId, label: device.label }))
	);
	await page.close();
	return outputs;
}

function compare(firstA, firstB, restartA) {
	const real = firstA.filter((output) => !PSEUDO_DEVICE_IDS.has(output.id));
	if (real.length === 0) return { verdict: 'UNKNOWN', reason: 'no real audiooutput enumerated' };
	if (firstA.some((output) => output.label === '')) {
		return { verdict: 'UNKNOWN', reason: 'labels hidden; the permission grant did not unlock them' };
	}
	const byLabel = (rows) => new Map(rows.map((row) => [row.label, row]));
	const [b, restart] = [byLabel(firstB), byLabel(restartA)];
	const rows = real.map((output) => ({
		label: output.label,
		id_differs_across_origins: output.id !== b.get(output.label)?.id,
		id_stable_same_origin_after_restart: output.id === restart.get(output.label)?.id,
		group_id_differs_across_origins: output.group_id !== b.get(output.label)?.group_id,
		label_present_under_both_origins: b.has(output.label)
	}));
	const all = (key) => rows.every((row) => row[key]);
	const none = (key) => rows.every((row) => !row[key]);
	let verdict = 'MIXED';
	if (all('id_differs_across_origins') && all('id_stable_same_origin_after_restart')) verdict = 'SALTED_PER_ORIGIN';
	else if (none('id_differs_across_origins') && all('id_stable_same_origin_after_restart')) verdict = 'NOT_SALTED';
	else if (!all('id_stable_same_origin_after_restart')) verdict = 'ROTATES_PER_LAUNCH';
	return { verdict, rows };
}

const profile = mkdtempSync(join(tmpdir(), 'odj-device-id-probe-'));
const [serverA, serverB] = [await serveBlankPage(), await serveBlankPage()];
const originA = `http://127.0.0.1:${serverA.address().port}`;
const originB = `http://127.0.0.1:${serverB.address().port}`;
const channel = values.channel === 'chromium' ? undefined : values.channel;
const launch = () => chromium.launchPersistentContext(profile, { channel, headless: true });
let result;
try {
	let context = await launch();
	const firstA = await enumerateOutputs(context, originA);
	const firstB = await enumerateOutputs(context, originB);
	await context.close();
	context = await launch();
	const restartA = await enumerateOutputs(context, originA);
	await context.close();
	result = compare(firstA, firstB, restartA);
} finally {
	serverA.close();
	serverB.close();
	rmSync(profile, { recursive: true, force: true });
}
console.log(JSON.stringify({ channel: values.channel, origins: [originA, originB], ...result }, null, 2));
if (result.verdict === 'UNKNOWN') process.exit(2);
