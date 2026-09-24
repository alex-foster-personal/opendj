/** Opens the audio I/O cluster and surfaces the MIDI entry (CHROME-07). */
export const ioSurface: { open: boolean } = $state({ open: false });

export function openIoView(): void {
	ioSurface.open = true;
}

export function closeIoView(): void {
	ioSurface.open = false;
}
