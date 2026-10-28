/**
 * The right-click rows on a USB stick's tree (USBEXP-04): USB status, Sync to
 * stick (disabled, says why) and Eject stick.
 *
 * One binding for every workspace. The stick tree (UsbStickTree) opens this
 * menu itself and stops the event, so Library mode (CrateDesk -> BrowserPanel)
 * and Gig mode (/performance, where Quick Draw would otherwise claim the
 * right-click) show the same rows from the same registry entry.
 *
 * Both runners open the USB export screen, so an eject's result (or
 * diskutil's refusal) is shown where the stick is listed.
 */
import { bindMenuItems, type BoundMenuItem } from '$lib/context-menu/bind';
import type { ResolvedMenuItem } from '$lib/context-menu/registry';
import { tick } from 'svelte';

import { ejectUsbStick, exportTargets, openUsbExport, stickActionBlocker, usbExportScreenMounted } from '$lib/rb/usb-export.svelte';
import { loadUsbExportScreen } from '$lib/rb/usb-export-screen-load';
import { announceUsbProblem, type UsbDiscoveryState, type UsbVolumeKnown } from '$lib/rb/usb-tracker.svelte';

/** The registry's `menuFor`, narrowed to the one target this menu reads, so the
 * caller can pass the lazily loaded registry (lazy.svelte.ts). */
export type StickMenuFor = (target: { kind: 'tree-readonly' }, ctx: { stick: boolean }) => ResolvedMenuItem[];

/** What the menu reads from the USB tracker: the listing and whether it is a
 * current, healthy scan (the same test the export screen uses). */
export interface StickTrackerView {
	volumes: readonly UsbVolumeKnown[];
	scannedAt: number | null;
	discovery: UsbDiscoveryState;
}

export const EJECT_UNVERIFIED =
	'not offered: the latest USB scan does not list this stick as mounted at this path, so Open DJ cannot tell what an eject would unmount';

/** The mount path an eject may target: the tree's stick only when this
 * session's latest healthy scan lists the same stick (same id) mounted at the
 * same path. Remembered sticks, a failed or not-yet-run scan, a path that
 * now belongs to another stick, and a listing id with no volume UUID (the
 * daemon refuses that eject) all give null. */
export function ejectableMountPath(volume: Pick<UsbVolumeKnown, 'id' | 'mount_path'>, tracker: StickTrackerView): string | null {
	const mount = volume.mount_path ?? null;
	if (mount === null || mount === '' || stickActionBlocker(volume.id) !== null) return null;
	const live = exportTargets(tracker.volumes, tracker).find((v) => v.id === volume.id && v.mount_path === mount);
	return live === undefined ? null : mount;
}

const SETTLE_ATTEMPTS = 3;

/** The steps of a menu Eject, passed in so the order can be tested without a
 * stick: load the screen, let it render, then eject only if it is mounted. */
export interface EjectSteps {
	load: () => Promise<unknown>;
	settle: () => Promise<void>;
	screenMounted: () => boolean;
	eject: () => Promise<void>;
	/** Says why the eject did not start, where the user sees it whether or
	 * not the screen's mount point is still there to show its own error. */
	notify: (message: string) => void;
}

export const EJECT_NOT_STARTED_LOAD = 'Eject not started: the USB export screen failed to load';
export const EJECT_NOT_STARTED_UNMOUNTED =
	'Eject not started: the USB export screen closed before it could show the eject, so the stick is still mounted';

/**
 * Starts the eject only once the USB export screen is mounted: it shows the
 * keep-plugged-in progress, makes the page inert and vetoes navigation. If the
 * screen cannot load, nothing is ejected (Codex P1 4287034869). If it loaded
 * but is not mounted after the render (a navigation unmounted its loader
 * during the load), nothing is ejected either, since nothing would hold the
 * page or say when the stick is safe to pull (Codex P1 4291127157). Either way
 * the user is told through `notify`, since a loader a navigation took away is
 * not there to show its own error (Sol P1 4398029451).
 * Returns whether the eject was started.
 */
export async function ejectOnceScreenIsUp(steps: EjectSteps): Promise<boolean> {
	try {
		await steps.load();
	} catch (exc: unknown) {
		const reason = exc instanceof Error ? exc.message : String(exc);
		console.error('[usb-export] eject not started: the screen failed to load', exc);
		steps.notify(`${EJECT_NOT_STARTED_LOAD} (${reason}). The stick is still mounted.`);
		return false;
	}
	// The loader sets the screen when the same load resolves, so one render
	// normally mounts it; a few are allowed before giving up.
	for (let i = 0; i < SETTLE_ATTEMPTS && !steps.screenMounted(); i += 1) await steps.settle();
	if (!steps.screenMounted()) {
		console.warn('[usb-export] eject not started: the USB export screen is not mounted to show it');
		steps.notify(EJECT_NOT_STARTED_UNMOUNTED);
		return false;
	}
	await steps.eject();
	return true;
}

export function usbStickMenuItems(
	menuFor: StickMenuFor,
	volume: Pick<UsbVolumeKnown, 'id' | 'mount_path'>,
	tracker: StickTrackerView
): BoundMenuItem[] {
	const mount = volume.mount_path ?? null;
	const ejectable = ejectableMountPath(volume, tracker);
	const items = bindMenuItems(menuFor({ kind: 'tree-readonly' }, { stick: mount !== null && mount !== '' }), {
		'usb-status': () => openUsbExport(),
		'usb-eject':
			ejectable === null
				? undefined
				: async () => {
						openUsbExport();
						await ejectOnceScreenIsUp({
							load: loadUsbExportScreen,
							settle: tick,
							screenMounted: usbExportScreenMounted,
							eject: () => ejectUsbStick(ejectable, volume.id),
							notify: announceUsbProblem
						});
					}
	});
	const why = stickActionBlocker(volume.id) ?? EJECT_UNVERIFIED;
	return items.map((item) => (item.id === 'usb-eject' && item.run === undefined ? { ...item, title: why } : item));
}
