/**
 * CUEOUT-22 relay worker: cue bridge PCM in from a MessagePort, out over the
 * shell's loopback WebSocket; control JSON both ways. Lives off the main thread
 * so page work never starves the headphones. See cue-native-sink.ts.
 *
 * PCM goes out as the platform's Float32Array bytes. Every Mac the app ships
 * for (Apple silicon and Intel) is little-endian, which is what the shell
 * decodes (`decode_pcm`).
 */

import { PcmBatcher, type NativeCueWorkerMessage, type NativeCueWorkerRequest } from './cue-native-sink';

let socket: WebSocket | null = null;
let pcmPort: MessagePort | null = null;
const batcher = new PcmBatcher();

function reply(message: NativeCueWorkerMessage): void {
	self.postMessage(message);
}

function connect(url: string, token: string): void {
	if (socket !== null) throw new Error('native cue relay is already connected');
	const ws = new WebSocket(`${url}?token=${encodeURIComponent(token)}`);
	ws.binaryType = 'arraybuffer';
	ws.onopen = () => reply({ kind: 'open' });
	ws.onmessage = (event: MessageEvent) => {
		if (typeof event.data !== 'string') return;
		try {
			reply({ kind: 'message', payload: JSON.parse(event.data) as Record<string, unknown> });
		} catch (error) {
			reply({ kind: 'closed', reason: `unparseable shell message: ${String(error)}` });
			ws.close();
		}
	};
	ws.onclose = (event: CloseEvent) => {
		socket = null;
		reply({ kind: 'closed', reason: `socket closed (${event.code}${event.reason ? `: ${event.reason}` : ''})` });
	};
	ws.onerror = () => {
		// onclose follows with the code; this only marks that it was an error.
	};
	socket = ws;
}

function attachPort(port: MessagePort): void {
	pcmPort?.close();
	batcher.reset();
	pcmPort = port;
	port.onmessage = (event: MessageEvent) => {
		const chunk = event.data;
		if (!(chunk instanceof Float32Array)) return;
		const ws = socket;
		if (ws === null || ws.readyState !== WebSocket.OPEN) return;
		for (const batch of batcher.push(chunk)) ws.send(batch);
	};
	port.start();
}

self.onmessage = (event: MessageEvent<NativeCueWorkerRequest>) => {
	const request = event.data;
	switch (request.kind) {
		case 'connect':
			connect(request.url, request.token);
			return;
		case 'command':
			if (socket === null || socket.readyState !== WebSocket.OPEN) {
				reply({ kind: 'closed', reason: 'command sent while the socket is not open' });
				return;
			}
			socket.send(JSON.stringify(request.payload));
			return;
		case 'pcm-port':
			attachPort(request.port);
			return;
		case 'pcm-detach':
			pcmPort?.close();
			pcmPort = null;
			batcher.reset();
			return;
	}
};
