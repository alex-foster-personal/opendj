/**
 * CUEOUT-22 (IOPIN-05): the installed macOS app's headphone cue output.
 *
 * WKWebView has no `AudioContext.setSinkId`, so the Chrome route (a second
 * AudioContext pinned to the headphones, CUEOUT-09) cannot exist in the shipped
 * app. The shell instead serves a loopback WebSocket (`apps/desktop/src-tauri/
 * src/cue_sink.rs`) that plays PCM on a CoreAudio device it opens itself. The
 * page keeps the whole monitor graph; only the last hop changes:
 *
 *   cue bridge sender worklet --MessagePort--> worker --WebSocket--> shell ring --> AUHAL
 *
 * The relay runs in a dedicated worker so main-thread work (rendering, GC)
 * cannot starve the headphones. The same socket carries the control plane the
 * page would otherwise get from `navigator.mediaDevices`: list outputs, open
 * or close the cue device, and pin MASTER as the macOS default output.
 *
 * The shell announces the socket through `globalThis.OPENDJ_CUE_SINK`, set by
 * its initialization script. No global means no native route (Chrome, or a
 * shell that failed to start the sink), and the caller keeps the Chrome path.
 */

/** Frames per socket message. 256 = two render quanta, ~5.3 ms at 48 kHz:
 * small enough to keep the relay's contribution to cue latency negligible,
 * large enough that per-message overhead does not matter. */
export const NATIVE_CUE_BATCH_FRAMES = 256;

/** Prefix that keeps a native device id from ever matching a browser
 * `deviceId` from Chrome, since both persist in the same mixer config. */
export const NATIVE_DEVICE_ID_PREFIX = 'native:';

export interface NativeCueSinkConfig {
	url: string;
	token: string;
}

/** One output as the shell lists it. */
export interface NativeOutputDevice {
	uid: string;
	name: string;
	channels: number;
	transport: string;
	is_default: boolean;
}

/** What the shell reports when it opens a cue device. */
export interface NativeOpenedDevice {
	name: string | null;
	client_sample_rate: number;
	device_sample_rate: number | null;
	device_latency_ms: number | null;
}

/** Events the shell pushes without being asked. */
export type NativeCueSinkEvent =
	| { type: 'devices_changed'; devices: NativeOutputDevice[] }
	| { type: 'device_lost'; uid: string }
	| { type: 'stats'; uid: string; ring: Record<string, number | boolean> }
	| { type: 'master_reasserted'; uid: string; from_uid: string | null }
	| { type: 'disconnected'; reason: string };

/** Read the shell's announcement. Throws on a malformed one: a shell that set
 * the global and got it wrong is a bug to surface, not a reason to fall back. */
export function nativeCueSinkConfig(globalObject: unknown = globalThis): NativeCueSinkConfig | null {
	const raw = (globalObject as { OPENDJ_CUE_SINK?: unknown }).OPENDJ_CUE_SINK;
	if (raw === undefined || raw === null) return null;
	const { url, token } = raw as { url?: unknown; token?: unknown };
	if (typeof url !== 'string' || !/^ws:\/\/127\.0\.0\.1:\d+\//.test(url)) {
		throw new Error(`OPENDJ_CUE_SINK.url must be a loopback ws:// URL, got ${String(url)}`);
	}
	if (typeof token !== 'string' || token.length === 0) {
		throw new Error('OPENDJ_CUE_SINK.token is missing');
	}
	return { url, token };
}

export function nativeDeviceId(uid: string): string {
	return `${NATIVE_DEVICE_ID_PREFIX}${uid}`;
}

/** The shell uid behind a native device id. Throws for any other id: handing
 * a browser deviceId to the shell would open nothing and say nothing. */
export function nativeDeviceUid(deviceId: string): string {
	if (!deviceId.startsWith(NATIVE_DEVICE_ID_PREFIX) || deviceId.length === NATIVE_DEVICE_ID_PREFIX.length) {
		throw new Error(`${deviceId} is not a native output id`);
	}
	return deviceId.slice(NATIVE_DEVICE_ID_PREFIX.length);
}

/** Shell listing -> the `{ id, label }` rows the I/O selects render. */
export function nativeOutputsAsHeadphoneOutputs(devices: readonly NativeOutputDevice[]): { id: string; label: string }[] {
	return devices.map((device) => ({ id: nativeDeviceId(device.uid), label: device.name }));
}

/**
 * Collects 128-frame worklet chunks (interleaved stereo) into fixed batches.
 * Pure and allocation-bounded so the worker's hot path is unit-testable.
 */
export class PcmBatcher {
	private readonly batch: Float32Array;
	private filled = 0;

	constructor(private readonly frames = NATIVE_CUE_BATCH_FRAMES) {
		if (!Number.isInteger(frames) || frames <= 0) throw new RangeError(`batch frames must be positive, got ${frames}`);
		this.batch = new Float32Array(frames * 2);
	}

	/** Append one chunk; returns every completed batch as its own buffer. */
	push(chunk: Float32Array): ArrayBuffer[] {
		if (chunk.length % 2 !== 0) throw new RangeError('PCM chunk must be whole stereo frames');
		const done: ArrayBuffer[] = [];
		let offset = 0;
		while (offset < chunk.length) {
			const take = Math.min(this.batch.length - this.filled, chunk.length - offset);
			this.batch.set(chunk.subarray(offset, offset + take), this.filled);
			this.filled += take;
			offset += take;
			if (this.filled === this.batch.length) {
				done.push(this.batch.slice().buffer);
				this.filled = 0;
			}
		}
		return done;
	}

	reset(): void {
		this.filled = 0;
	}
}

/** Messages between the client and its worker. */
export type NativeCueWorkerRequest =
	| { kind: 'connect'; url: string; token: string }
	| { kind: 'command'; payload: Record<string, unknown> }
	| { kind: 'pcm-port'; port: MessagePort }
	| { kind: 'pcm-detach' };

export type NativeCueWorkerMessage =
	| { kind: 'open' }
	| { kind: 'message'; payload: Record<string, unknown> }
	| { kind: 'closed'; reason: string };
