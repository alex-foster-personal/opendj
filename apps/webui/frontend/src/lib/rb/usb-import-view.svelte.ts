/**
 * What the USB panel shows after Import. The heavy read (stick library,
 * deck state) stays in usb-import-open.ts so this module can sit on the
 * performance first paint.
 */
import type { RankedUsbImport, UsbImportPhaseCounts, UsbImportPlan } from './usb-import-rank';

export type UsbImportViewStatus = 'idle' | 'reading' | 'ready' | 'error';

const EMPTY_COUNTS: UsbImportPhaseCounts = {
	next: 0,
	soon: 0,
	ready: 0,
	later: 0,
	skip: 0
};

export const usbImportView = $state({
	open: false,
	status: 'idle' as UsbImportViewStatus,
	volumeId: '',
	volumeName: '',
	headline: '',
	performing: false,
	next: [] as RankedUsbImport[],
	queue: [] as RankedUsbImport[],
	counts: { ...EMPTY_COUNTS },
	error: ''
});

let token = 0;

export function beginUsbImportPlan(volumeId: string, volumeName: string): number {
	token += 1;
	usbImportView.open = true;
	usbImportView.status = 'reading';
	usbImportView.volumeId = volumeId;
	usbImportView.volumeName = volumeName;
	usbImportView.headline = 'Reading the stick to rank its tracks. Analysis runs in library order.';
	usbImportView.performing = false;
	usbImportView.next = [];
	usbImportView.queue = [];
	usbImportView.counts = { ...EMPTY_COUNTS };
	usbImportView.error = '';
	return token;
}

export function usbImportPlanCurrent(mine: number): boolean {
	return mine === token;
}

export function publishUsbImportPlan(mine: number, plan: UsbImportPlan): void {
	if (mine !== token) return;
	usbImportView.status = 'ready';
	usbImportView.headline = plan.headline;
	usbImportView.performing = plan.performing;
	usbImportView.next = plan.next;
	usbImportView.queue = plan.queue;
	usbImportView.counts = plan.counts;
	usbImportView.error = '';
}

export function failUsbImportPlan(mine: number, message: string): void {
	if (mine !== token) return;
	usbImportView.status = 'error';
	usbImportView.error = message;
	usbImportView.headline = 'The stick is queued. It could not be ranked.';
	usbImportView.next = [];
	usbImportView.queue = [];
	usbImportView.counts = { ...EMPTY_COUNTS };
}

export function closeUsbImportPlan(): void {
	token += 1;
	usbImportView.open = false;
	usbImportView.status = 'idle';
	usbImportView.headline = '';
	usbImportView.performing = false;
	usbImportView.next = [];
	usbImportView.queue = [];
	usbImportView.counts = { ...EMPTY_COUNTS };
	usbImportView.error = '';
}
