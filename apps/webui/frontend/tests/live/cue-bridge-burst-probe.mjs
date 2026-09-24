/**
 * Does the cue bridge deliver a SHORT BURST intact, or does priming eat its head?
 *
 * The receiver outputs silence until the ring holds TARGET_FRAMES, and returns
 * to priming after any underrun. A continuous tone pays that once and streams
 * clean forever (which is why the audible probe passed). A 200 ms chirp into an
 * idle ring may lose its opening and be holed mid-sweep, which destroys the
 * correlation peak without making the bus quiet.
 *
 * Measures the delivered burst against the reference by the SAME normalized
 * cross-correlation the app uses, entirely in the digital domain: the receiver
 * is captured by a recorder worklet in the cue context, so no microphone, no
 * room and no Bluetooth is involved. Any shortfall here is the bridge alone.
 *
 * Control: the identical burst correlated against itself through a plain gain
 * node in the same context. That must score ~1.00, or the probe is broken.
 */
import { chromium } from '@playwright/test';
const arg = (n, d = null) => { const i = process.argv.indexOf(`--${n}`); return i === -1 ? d : process.argv[i + 1]; };
const URL_ = String(arg('url', 'http://localhost:9464/performance'));

const browser = await chromium.launch({ headless: true, timeout: 600_000,
	args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream'] });
try {
	const page = await browser.newPage();
	page.on('console', (m) => console.log(`  page: ${m.text()}`));
	await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 180_000 });
	const out = await page.evaluate(async () => {
		const recSrc = `
			class Rec extends AudioWorkletProcessor {
				constructor() { super(); this.buf = []; this.on = false;
					this.port.onmessage = (e) => {
						if (e.data === 'go') { this.on = true; this.buf = []; }
						else if (e.data === 'stop') { this.on = false;
							this.port.postMessage(Float32Array.from(this.buf)); } }; }
				process(inputs) {
					const ch = inputs[0] && inputs[0][0];
					if (this.on && ch) for (let i = 0; i < ch.length; i += 1) this.buf.push(ch[i]);
					return true; }
			}
			registerProcessor('burst-rec', Rec);
		`;
		const recUrl = URL.createObjectURL(new Blob([recSrc], { type: 'text/javascript' }));
		const bridgeUrl = new URL('/src/lib/player/cue-bridge-processor.js', location.origin).href;
		const main = new AudioContext();
		const cue = new AudioContext({ sampleRate: main.sampleRate });
		for (const c of [main, cue]) { await c.audioWorklet.addModule(bridgeUrl); await c.audioWorklet.addModule(recUrl); }
		await main.resume(); await cue.resume();

		// The real probe: exponential sweep, same shape as cue-latency.ts.
		const sr = main.sampleRate, T = 0.2, f0 = 300, f1 = 6000, fade = 0.01;
		const n = Math.round(sr * T), K = Math.log(f1 / f0);
		const ref = new Float32Array(n);
		for (let i = 0; i < n; i += 1) {
			const t = i / sr;
			let a = 0.9;
			if (t < fade) a *= 0.5 - 0.5 * Math.cos(Math.PI * t / fade);
			if (t > T - fade) a *= 0.5 - 0.5 * Math.cos(Math.PI * (T - t) / fade);
			ref[i] = a * Math.sin((2 * Math.PI * f0 * T / K) * (Math.exp(t * K / T) - 1));
		}
		const ch = new MessageChannel(); ch.port1.start(); ch.port2.start();
		const sender = new AudioWorkletNode(main, 'cue-bridge-sender',
			{ numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit',
			  processorOptions: { mode: 'port' } });
		const receiver = new AudioWorkletNode(cue, 'cue-bridge-receiver',
			{ numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2],
			  processorOptions: { mode: 'port', capacity: 16384 } });
		sender.port.postMessage({ type: 'connect', port: ch.port1 }, [ch.port1]);
		receiver.port.postMessage({ type: 'connect', port: ch.port2 }, [ch.port2]);
		let underruns = 0;
		receiver.port.onmessage = (e) => { if (e.data?.type === 'underrun') underruns += 1; };
		const rec = new AudioWorkletNode(cue, 'burst-rec', { numberOfInputs: 1, numberOfOutputs: 1 });
		const sink = cue.createGain(); sink.gain.value = 0;
		receiver.connect(rec); rec.connect(sink); sink.connect(cue.destination);

		const play = (target) => { const b = main.createBuffer(1, n, sr); b.copyToChannel(ref, 0);
			const s = main.createBufferSource(); s.buffer = b; s.connect(target); s.start(); return s; };
		const grab = () => new Promise((r) => { rec.port.onmessage = (e) => r(e.data); rec.port.postMessage('stop'); });

		const ncc = (cap, refr) => {
			const m = cap.length - refr.length; if (m <= 0) return 0;
			let rn = 0; for (const v of refr) rn += v * v; rn = Math.sqrt(rn);
			let best = 0;
			for (let lag = 0; lag <= m; lag += 1) {
				let num = 0, cn = 0;
				for (let i = 0; i < refr.length; i += 1) { const c = cap[lag + i]; num += c * refr[i]; cn += c * c; }
				const den = Math.sqrt(cn) * rn;
				if (den > 1e-12) best = Math.max(best, Math.abs(num) / den);
			}
			return best;
		};

		// SUBJECT: burst through the bridge, from an idle (drained) ring.
		await new Promise((r) => setTimeout(r, 400));
		rec.port.postMessage('go'); play(sender);
		await new Promise((r) => setTimeout(r, 1200));
		const through = await grab();
		const subject = ncc(through, ref);
		let energy = 0; for (const v of through) energy += v * v;

		// CONTROL: same burst, same context, no bridge.
		const direct = cue.createGain(); direct.gain.value = 1;
		const b2 = cue.createBuffer(1, n, sr); b2.copyToChannel(ref, 0);
		rec.port.postMessage('go');
		const s2 = cue.createBufferSource(); s2.buffer = b2; s2.connect(rec); s2.start();
		await new Promise((r) => setTimeout(r, 1200));
		const ctrl = await grab();
		const control = ncc(ctrl, ref);

		await main.close(); await cue.close();
		return { subject: Number(subject.toFixed(3)), control: Number(control.toFixed(3)),
			underruns, deliveredFrames: through.length, energy: Number(energy.toFixed(4)),
			targetFramesMs: Number((2048 / sr * 1000).toFixed(1)) };
	});
	console.log(JSON.stringify(out, null, 1));
	console.log(out.control < 0.9 ? 'PROBE BROKEN: control did not score ~1.0'
		: out.subject < 0.35 ? `CONFIRMED: the bridge mangles a 200 ms burst (${out.subject} against the 0.35 refusal line) with NO mic, NO room and NO Bluetooth involved.`
		: `NOT the cause: the bridge delivers the burst intact (${out.subject}).`);
} finally { await browser.close(); }
