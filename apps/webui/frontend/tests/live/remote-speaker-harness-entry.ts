/**
 * Browser half of the remote speaker loopback proof: bundled by
 * tests/live/remote-speaker-loopback.mjs with esbuild and loaded into both
 * Chromium pages. Not imported by app code.
 */
import { runRemoteSpeakerCommand, type RemoteSpeakerEnv } from '$lib/player/remote-speaker-orders';
import {
	remoteSpeakerReceiverLevel,
	remoteSpeakerReceiverStream,
	remoteSpeakerSenderStats
} from '$lib/player/remote-speaker';

interface SentStats {
	bytes: number;
	at: number;
	mime: string | undefined;
	clock: number | undefined;
	channels: number | undefined;
	fmtp: string | undefined;
}

type StepResult = { status: 'succeeded'; result?: { sdp: string } } | { status: 'failed'; error: string };

/** What the loopback driver reads and calls through page.evaluate. */
interface HarnessGlobals {
	__ctx: AudioContext;
	__master: GainNode;
	__orders: string[];
	__ready?: boolean;
	__loopError?: string;
	__onsetCtx?: AudioContext;
	__level: typeof remoteSpeakerReceiverLevel;
	__sent: () => Promise<SentStats | null>;
	__watchOnset: (floor: number, timeoutMs: number) => Promise<number>;
	__step: (on: boolean) => number;
}

const w = globalThis as typeof globalThis & Partial<HarnessGlobals>;
const ctx = new AudioContext({ sampleRate: 48000 });
const master = ctx.createGain();
w.__ctx = ctx;
w.__master = master;
master.gain.value = 0;
const tone = ctx.createOscillator();
tone.frequency.value = 440;
tone.connect(master);
tone.start();
master.connect(ctx.destination);
const orders: string[] = [];
w.__orders = orders;

const env: RemoteSpeakerEnv = {
	flag: 'on',
	engine: { apiBase: '', fetch: (input, init) => fetch(input, init) },
	masterTap: () => ({ context: ctx, node: master }),
	masterSinkId: () => null
};

async function loop(): Promise<void> {
	const published = await fetch('/api/v1/state/ui-mirror', {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ client_open: true })
	});
	if (published.status !== 202) throw new Error('mirror publish answered ' + published.status);
	w.__ready = true;
	for (;;) {
		const claimed = await fetch('/api/v1/commands/next?wait_ms=5000');
		const order = (await claimed.json()) as { id: string; payload: Record<string, unknown> } | null;
		if (order === null) continue;
		orders.push(String(order.payload.type));
		let step: StepResult;
		try {
			const result = await runRemoteSpeakerCommand(order.payload, env);
			step = result === null ? { status: 'succeeded' } : { status: 'succeeded', result };
		} catch (error) {
			step = { status: 'failed', error: error instanceof Error ? error.message : String(error) };
		}
		await fetch('/api/v1/commands/' + order.id + '/result', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ steps: [step], mirror_delta: { changed: {} } })
		});
	}
}

w.__level = () => remoteSpeakerReceiverLevel();
/** The sent codec as negotiated, and the bytes sent so far. */
w.__sent = async () => {
	const report = await remoteSpeakerSenderStats();
	if (report === null) return null;
	let out: SentStats | null = null;
	report.forEach((stat: RTCOutboundRtpStreamStats) => {
		if (stat.type === 'outbound-rtp' && stat.kind === 'audio') {
			const codec = (stat.codecId === undefined ? undefined : report.get(stat.codecId)) as
				| RTCRtpCodec
				| undefined;
			out = {
				bytes: stat.bytesSent ?? 0,
				at: stat.timestamp,
				mime: codec?.mimeType,
				clock: codec?.clockRate,
				channels: codec?.channels,
				fmtp: codec?.sdpFmtpLine
			};
		}
	});
	return out;
};
/** Onset probe: a second meter on the received stream; resolves with the wall-clock ms of the first loud block. */
w.__watchOnset = (floor, timeoutMs) =>
	new Promise<number>((resolve, reject) => {
		const stream = remoteSpeakerReceiverStream();
		if (stream === null) {
			reject(new Error('no received stream'));
			return;
		}
		const onsetCtx = (w.__onsetCtx ??= new AudioContext({ sampleRate: 48000 }));
		const analyser = onsetCtx.createAnalyser();
		analyser.fftSize = 256;
		const source = onsetCtx.createMediaStreamSource(stream);
		source.connect(analyser);
		const block = new Float32Array(128);
		const deadline = performance.now() + timeoutMs;
		const tick = () => {
			analyser.getFloatTimeDomainData(block);
			let peak = 0;
			for (const v of block) peak = Math.max(peak, Math.abs(v));
			if (peak > floor) {
				source.disconnect();
				resolve(performance.timeOrigin + performance.now());
				return;
			}
			if (performance.now() > deadline) {
				source.disconnect();
				reject(new Error('no onset within ' + timeoutMs + ' ms'));
				return;
			}
			setTimeout(tick, 1);
		};
		tick();
	});
w.__step = (on) => {
	master.gain.setValueAtTime(on ? 0.5 : 0, ctx.currentTime);
	return performance.timeOrigin + performance.now();
};
loop().catch((error: unknown) => {
	w.__loopError = String(error);
});
