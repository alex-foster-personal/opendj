/** Real tailnet acceptance: both HTTP names, TLS, JS, AudioWorklet, IPC, API. */

import http from 'node:http';
import net from 'node:net';

import { chromium } from '@playwright/test';

function argumentsFrom(argv) {
	const result = {};
	for (let index = 0; index < argv.length; index += 2) {
		const key = argv[index];
		const value = argv[index + 1];
		if (!key?.startsWith('--') || value === undefined) {
			throw new Error(`expected --key value arguments, got ${argv.join(' ')}`);
		}
		result[key.slice(2)] = value;
	}
	return result;
}

function assertInputs(host, ip) {
	if (!/^[a-z0-9.-]+$/.test(host) || !host.endsWith('.ts.net')) {
		throw new Error(`refusing non-MagicDNS host ${JSON.stringify(host)}`);
	}
	if (net.isIP(ip) !== 4) throw new Error(`expected a Tailscale IPv4 address, got ${ip}`);
}

function httpRedirect(ip, requestHost, path) {
	return new Promise((resolve, reject) => {
		const request = http.request(
			{
				host: ip,
				port: 80,
				path,
				method: 'GET',
				headers: { Host: requestHost },
				timeout: 5_000
			},
			(response) => {
				response.resume();
				response.once('end', () => {
					resolve({ status: response.statusCode ?? 0, location: response.headers.location ?? null });
				});
			}
		);
		request.once('timeout', () => request.destroy(new Error(`HTTP ${requestHost} timed out`)));
		request.once('error', reject);
		request.end();
	});
}

async function main() {
	const args = argumentsFrom(process.argv.slice(2));
	const host = args.host;
	const ip = args.ip;
	assertInputs(host, ip);
	const path = '/performance';
	const secureUrl = `https://${host}${path}`;
	const redirects = {};
	for (const requestHost of ['agentbox', host]) {
		const response = await httpRedirect(ip, requestHost, path);
		if (response.status !== 307 || response.location !== secureUrl) {
			throw new Error(
				`http://${requestHost}${path} returned ${response.status} ${response.location}, expected 307 ${secureUrl}`
			);
		}
		redirects[requestHost] = response;
	}

	const browser = await chromium.launch({
		headless: true,
		args: [
			`--host-resolver-rules=MAP ${host} ${ip}`,
			'--autoplay-policy=no-user-gesture-required'
		]
	});
	try {
		const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
		const pageErrors = [];
		page.on('pageerror', (error) => pageErrors.push(error.stack ?? error.message));
		const healthResponse = page.waitForResponse(
			(response) => response.url().includes('/api/v1/health') && response.status() === 200,
			{ timeout: 30_000 }
		);
		const visitorResponse = page.waitForResponse(
			(response) => response.url().includes('/api/v1/client-events'),
			{ timeout: 30_000 }
		);
		const navigation = await page.goto(secureUrl, {
			waitUntil: 'domcontentloaded',
			timeout: 30_000
		});
		if (navigation === null || navigation.status() !== 200) {
			throw new Error(`browser navigation returned ${navigation?.status() ?? 'no response'}`);
		}
		const [health, visitor] = await Promise.all([healthResponse, visitorResponse]);
		if (visitor.status() !== 202) {
			throw new Error(`visitor telemetry returned ${visitor.status()}`);
		}
		await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, null, {
			timeout: 30_000
		});
		const browserState = await page.evaluate(() => ({
			url: window.location.href,
			secure_context: window.isSecureContext,
			audio_worklet_node: typeof AudioWorkletNode,
			audio_context_worklet:
				typeof AudioContext === 'function' && new AudioContext().audioWorklet !== undefined,
			ipc: window.musicDjToolsPerformance !== undefined,
			visible_audio_error: document.body.innerText.includes('AudioWorklet is unavailable')
		}));
		if (
			browserState.url !== secureUrl ||
			browserState.secure_context !== true ||
			browserState.audio_worklet_node !== 'function' ||
			browserState.audio_context_worklet !== true ||
			browserState.ipc !== true ||
			browserState.visible_audio_error !== false
		) {
			throw new Error(`browser capability failure: ${JSON.stringify(browserState)}`);
		}
		let stemDeck = null;
		if (args['stem-track'] !== undefined) {
			const loaded = await page.evaluate(async (stableId) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				return Promise.race([
					ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId }),
					new Promise((_, reject) =>
						setTimeout(() => reject(new Error(`stem track ${stableId} load timed out`)), 120_000)
					)
				]);
			}, args['stem-track']);
			const deck = loaded.decks[1];
			stemDeck = {
				stable_id: deck.stable_id,
				status: deck.stems.status,
				source: deck.stems.source,
				layout: deck.stems.layout,
				controls: deck.stems.available_controls,
				error: deck.stems.error
			};
			if (
				stemDeck.stable_id !== args['stem-track'] ||
				stemDeck.status !== 'ready' ||
				stemDeck.layout === null ||
				stemDeck.controls.length === 0 ||
				stemDeck.error !== null
			) {
				throw new Error(`stem deck load failed: ${JSON.stringify(stemDeck)}`);
			}
			const muted = await page.evaluate(async () => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				return ipc.dispatch({ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true });
			});
			if (muted.decks[1].stems.controls.vocal.muted !== true) {
				throw new Error('real stem graph did not acknowledge vocal mute');
			}
			await page.evaluate(async () => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				await ipc.dispatch({ type: 'stem_mute', deck: 1, stem: 'vocal', muted: false });
			});
		}
		await page.waitForTimeout(500);
		if (pageErrors.length > 0) {
			throw new Error(`browser page errors: ${pageErrors.join('\n---\n')}`);
		}
		console.log(
			JSON.stringify({
				redirects,
				navigation_status: navigation.status(),
				health_status: health.status(),
				visitor_status: visitor.status(),
				browser: browserState,
				stem_deck: stemDeck
			})
		);
	} finally {
		await browser.close();
	}
}

await main();
