// The auto-update channel (OPS-15) on electron-updater.
//
// Same rule as the Tauri shell: the component that installs is the one that
// verifies, and it fetches the feed itself; it is never handed a manifest the
// engine or the page parsed. On macOS, Squirrel.Mac refuses a downloaded
// bundle whose code signature does not satisfy the running app's designated
// requirement, which is the role Tauri's minisign pubkey plays. An unsigned or
// unpackaged build therefore cannot install, and says so.

import type { ProgressInfo } from 'electron-updater';

/** Generic-provider feed: electron-updater reads `${UPDATE_FEED_URL}/latest-mac.yml`. */
export const UPDATE_FEED_URL = 'https://github.com/alex-foster-personal/issue-assets/releases/latest/download';

export type ApplyProgress =
	| { phase: 'checking' }
	| { phase: 'downloading'; received: number; total: number | null }
	| { phase: 'installing' }
	| { phase: 'restarting' };

export type ApplyOutcome = { kind: 'no-update' } | { kind: 'installed' } | { kind: 'refused'; reason: string };

export interface UpdaterHost {
	isPackaged: boolean;
	currentVersion: string;
	/** Called right before the installer quits the app; must lift the quit gate. */
	allowQuit: () => void;
}

export async function applyUpdate(host: UpdaterHost, onProgress: (progress: ApplyProgress) => void): Promise<ApplyOutcome> {
	if (!host.isPackaged) {
		return {
			kind: 'refused',
			reason: 'this is an unpackaged development build, which has no signed bundle to replace. Install a released build to update.'
		};
	}
	let autoUpdater: typeof import('electron-updater').autoUpdater;
	try {
		({ autoUpdater } = await import('electron-updater'));
	} catch (error) {
		return { kind: 'refused', reason: `the updater could not be loaded: ${(error as Error).message}` };
	}
	autoUpdater.autoDownload = false;
	autoUpdater.autoInstallOnAppQuit = false;
	autoUpdater.allowDowngrade = false;
	autoUpdater.setFeedURL({ provider: 'generic', url: UPDATE_FEED_URL });
	try {
		onProgress({ phase: 'checking' });
		const result = await autoUpdater.checkForUpdates();
		if (result === null || !result.isUpdateAvailable) return { kind: 'no-update' };
		onProgress({ phase: 'downloading', received: 0, total: null });
		const onDownload = (info: ProgressInfo): void =>
			onProgress({ phase: 'downloading', received: info.transferred, total: info.total || null });
		autoUpdater.on('download-progress', onDownload);
		try {
			await autoUpdater.downloadUpdate();
		} finally {
			autoUpdater.removeListener('download-progress', onDownload);
		}
		onProgress({ phase: 'installing' });
		onProgress({ phase: 'restarting' });
		host.allowQuit();
		autoUpdater.quitAndInstall(false, true);
		return { kind: 'installed' };
	} catch (error) {
		// A signature that does not verify, a 404, a bundle macOS will not
		// replace: surfaced verbatim, never retried silently.
		return { kind: 'refused', reason: error instanceof Error ? error.message : String(error) };
	}
}
