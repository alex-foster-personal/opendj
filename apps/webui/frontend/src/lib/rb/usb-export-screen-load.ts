/**
 * The one fetch of the lazily loaded USB export screen (USBEXP-01). The mount
 * point (UsbExportScreenLoader) and the stick menu's Eject share it, so an
 * eject never starts before the screen that shows its progress and blocks the
 * page is loaded (Codex P1 4287034869). A failed fetch stays failed for the
 * document, as the browser's module map keeps it.
 */
import type { Component } from 'svelte';

let pending: Promise<Component> | null = null;

export function loadUsbExportScreen(): Promise<Component> {
	pending ??= import('$lib/components/rb/UsbExportScreen.svelte').then((m) => m.default as Component);
	return pending;
}
