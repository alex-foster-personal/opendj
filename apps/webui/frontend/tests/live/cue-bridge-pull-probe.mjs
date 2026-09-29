/**
 * Is the cue bridge SENDER ever pulled?
 *
 * `bridgeSender` is an AudioWorkletNode with numberOfOutputs: 0, connected only
 * as a sink (bridgeInput -> sender) and never onward to a destination. A node
 * the renderer does not pull never writes to the ring, so the receiver starves
 * and the cue bus is silent while every setSinkId reports success.
 *
 * This asks the question directly, with a control: the SAME question against a
 * zero-output worklet that IS pulled through the destination. If the control
 * also reads zero, the probe is broken rather than the bridge.
 */
import { chromium } from '@playwright/test';
const URL_ = process.argv[2] ?? 'http://localhost:9464/performance';
const browser = await chromium.launch({ headless: true, timeout: 600_000,
	args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream'] });
try {
	const page = await browser.newPage();
	await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 180_000 });
	const out = await page.evaluate(async () => {
		const src = `
			class Counter extends AudioWorkletProcessor {
				constructor() { super(); this.n = 0;
					this.port.onmessage = () => this.port.postMessage(this.n); }
				process() { this.n += 1; return true; }
			}
			registerProcessor('pull-counter', Counter);
		`;
		const url = URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));
		const ctx = new AudioContext();
		await ctx.audioWorklet.addModule(url);
		await ctx.resume();
		const mk = () => new AudioWorkletNode(ctx, 'pull-counter',
			{ numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit' });
		// SUBJECT: fed by a running source, never connected onward. Same shape as bridgeSender.
		const subject = mk();
		const osc = ctx.createOscillator(); osc.connect(subject); osc.start();
		// CONTROL: an ordinary node that IS pulled, proving the counter works at all.
		const pulled = new AudioWorkletNode(ctx, 'pull-counter',
			{ numberOfInputs: 1, numberOfOutputs: 1, channelCount: 2, channelCountMode: 'explicit' });
		const mute = ctx.createGain(); mute.gain.value = 0;
		const osc2 = ctx.createOscillator(); osc2.connect(pulled); pulled.connect(mute);
		mute.connect(ctx.destination); osc2.start();
		await new Promise((r) => setTimeout(r, 1500));
		const ask = (n) => new Promise((r) => { n.port.onmessage = (e) => r(e.data); n.port.postMessage('?'); });
		const res = { subject: await ask(subject), control: await ask(pulled), state: ctx.state };
		await ctx.close();
		return res;
	});
	console.log(JSON.stringify(out));
	console.log(out.subject === 0
		? 'CONFIRMED: a zero-output worklet with no path to the destination is NEVER pulled.'
		: 'NOT the cause: the unconnected zero-output worklet does get pulled.');
	console.log(out.control > 0 ? 'control ok (counter works when pulled)' : 'PROBE BROKEN: control read zero too');
} finally { await browser.close(); }
