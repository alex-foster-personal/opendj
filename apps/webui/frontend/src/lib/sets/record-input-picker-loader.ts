/** Lazy load for unit bundles that stub this module instead of compiling the picker Svelte file. */
export type RecordInputPickerComponent =
	typeof import('../components/rb/browser/RecordInputPicker.svelte').default;

export async function loadRecordInputPicker(): Promise<RecordInputPickerComponent> {
	return (await import('../components/rb/browser/RecordInputPicker.svelte')).default;
}
