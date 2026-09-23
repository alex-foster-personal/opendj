/** Module-level handle so UI can request probes without importing instrumentation. */
import type { DeviceOutputProbeHandle } from './device-output-probe';

let _handle: DeviceOutputProbeHandle | null = null;

export function registerDeviceOutputProbe(handle: DeviceOutputProbeHandle | null): void {
	_handle = handle;
}

export async function switchDeviceOutput(): Promise<void> {
	await _handle?.switchOutput();
}
