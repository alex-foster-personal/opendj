/**
 * CUEOUT-22: the page side of the native cue sink, split from
 * `cue-native-sink.ts` so the client and its worker load only when the shell
 * announced a sink. `headphones.ts` imports it dynamically, which keeps it
 * out of the initial library chunk that every Chrome tab pays for.
 */
import {
	nativeDeviceUid,
	type NativeCueSinkConfig,
	type NativeCueSinkEvent,
	type NativeCueWorkerMessage,
	type NativeCueWorkerRequest,
	type NativeOpenedDevice,
	type NativeOutputDevice
} from './cue-native-sink';

type _Pending = { resolve: (value: Record<string, unknown>) => void; reject: (error: Error) => void };

/** The page side of the native sink: a request/response client over the worker. */
export class NativeCueSinkClient {
	private readonly worker: Worker;
	private nextId = 1;
	private readonly pending = new Map<number, _Pending>();
	private readonly listeners = new Set<(event: NativeCueSinkEvent) => void>();
	private readonly ready: Promise<void>;
	private closedReason: string | null = null;
	/** Set by the last successful `open`. */
	opened: NativeOpenedDevice | null = null;

	constructor(config: NativeCueSinkConfig, worker?: Worker) {
		this.worker =
			worker ?? new Worker(new URL('./cue-native-sink.worker.ts', import.meta.url), { type: 'module' });
		let markReady: () => void = () => {};
		let markFailed: (error: Error) => void = () => {};
		this.ready = new Promise<void>((resolve, reject) => {
			markReady = resolve;
			markFailed = reject;
		});
		// A rejection nobody awaited yet is still handled where it is awaited.
		this.ready.catch(() => {});
		this.worker.onmessage = (event: MessageEvent<NativeCueWorkerMessage>) => {
			const message = event.data;
			if (message.kind === 'open') {
				markReady();
				return;
			}
			if (message.kind === 'closed') {
				this.closedReason = message.reason;
				const error = new Error(`native cue sink disconnected: ${message.reason}`);
				markFailed(error);
				for (const waiter of this.pending.values()) waiter.reject(error);
				this.pending.clear();
				this.emit({ type: 'disconnected', reason: message.reason });
				return;
			}
			this.route(message.payload);
		};
		this.worker.onerror = (event: ErrorEvent) => {
			const reason = `worker error: ${event.message}`;
			this.closedReason = reason;
			markFailed(new Error(reason));
			this.emit({ type: 'disconnected', reason });
		};
		this.post({ kind: 'connect', url: config.url, token: config.token });
	}

	get disconnected(): string | null {
		return this.closedReason;
	}

	onEvent(listener: (event: NativeCueSinkEvent) => void): () => void {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	private emit(event: NativeCueSinkEvent): void {
		for (const listener of this.listeners) listener(event);
	}

	private post(request: NativeCueWorkerRequest, transfer: Transferable[] = []): void {
		this.worker.postMessage(request, transfer);
	}

	private route(payload: Record<string, unknown>): void {
		const id = typeof payload.id === 'number' ? payload.id : null;
		if (id !== null && this.pending.has(id)) {
			const waiter = this.pending.get(id) as _Pending;
			this.pending.delete(id);
			if (payload.type === 'error') waiter.reject(new Error(String(payload.message)));
			else waiter.resolve(payload);
			return;
		}
		switch (payload.type) {
			case 'devices_changed':
			case 'device_lost':
			case 'stats':
			case 'master_reasserted':
				this.emit(payload as NativeCueSinkEvent);
				return;
			case 'error':
				// An error with no request id is the shell refusing something it
				// could not attribute; never swallow it.
				console.error(`[native-cue] ${String(payload.message)}`);
				return;
			default:
				console.error(`[native-cue] unexpected message ${JSON.stringify(payload)}`);
		}
	}

	private async request(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
		await this.ready;
		if (this.closedReason !== null) throw new Error(`native cue sink disconnected: ${this.closedReason}`);
		const id = this.nextId++;
		const reply = new Promise<Record<string, unknown>>((resolve, reject) => {
			this.pending.set(id, { resolve, reject });
		});
		this.post({ kind: 'command', payload: { ...payload, id } });
		return reply;
	}

	async list(): Promise<NativeOutputDevice[]> {
		const reply = await this.request({ type: 'list' });
		if (!Array.isArray(reply.devices)) throw new Error('native cue sink listing has no devices array');
		return reply.devices as NativeOutputDevice[];
	}

	async open(deviceId: string, sampleRate: number): Promise<NativeOpenedDevice> {
		const reply = await this.request({ type: 'open', uid: nativeDeviceUid(deviceId), sample_rate: sampleRate });
		this.opened = reply.device as NativeOpenedDevice;
		return this.opened;
	}

	async close(): Promise<void> {
		this.opened = null;
		if (this.closedReason !== null) return;
		await this.request({ type: 'close' });
	}

	async setMaster(deviceId: string): Promise<void> {
		await this.request({ type: 'set_master', uid: nativeDeviceUid(deviceId) });
	}

	/** Hand the cue bridge sender's port to the worker, which relays its PCM. */
	attachPcmPort(port: MessagePort): void {
		this.post({ kind: 'pcm-port', port }, [port]);
	}

	detachPcm(): void {
		this.post({ kind: 'pcm-detach' });
	}

	/** Stop the worker; the shell closes the cue device when the socket drops. */
	dispose(): void {
		this.worker.terminate();
		this.closedReason ??= 'disposed';
		for (const waiter of this.pending.values()) waiter.reject(new Error('native cue sink disposed'));
		this.pending.clear();
		this.listeners.clear();
	}
}
