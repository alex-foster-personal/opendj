/**
 * USB volume import via the existing setup folder-import job.
 * Files stay on the stick; tags only, no analysis.
 */
import { startFolderImport } from '$lib/setup/setup-api';
import { pushToast } from '$lib/stores.svelte';
import { RbApiError } from './api-rb-error';
import { usbAccessBlocked, type UsbVolumeKnown } from './usb-tracker.svelte';

/** A blocked drive (USBPLAY-02) cannot be read, so its import would only
 * queue a job that fails on the first directory it lists. */
export function canImportUsbVolume(vol: UsbVolumeKnown): boolean {
	return Boolean(
		vol.mount_path &&
			vol.present &&
			!vol.simulated &&
			!vol.forgotten &&
			vol.is_music !== false &&
			!usbAccessBlocked(vol)
	);
}

export async function importUsbVolume(vol: UsbVolumeKnown): Promise<void> {
	if (!canImportUsbVolume(vol) || !vol.mount_path) return;
	try {
		await startFolderImport({ folders: [vol.mount_path] });
		pushToast('Import queued', 'info');
	} catch (err) {
		const msg =
			err instanceof RbApiError
				? err.message
				: err instanceof Error
					? err.message
					: String(err);
		pushToast(`USB import failed: ${msg}`, 'error');
	}
}
