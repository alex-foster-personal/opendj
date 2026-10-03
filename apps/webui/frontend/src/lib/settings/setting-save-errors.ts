/**
 * Where a settings write that failed on disk is reported (PR #4014, Sol P1
 * r4167041271). apply.ts reports here instead of importing the toast store
 * itself, which would add one more importer to the most-imported module in the
 * frontend (frontend.max_fan_in). app-init.ts, which already owns a toast
 * import, installs the toast sink at boot. Until then, the failure goes to
 * console.error, never nowhere.
 */
type SettingSaveErrorSink = (message: string, cause: unknown) => void;

let sink: SettingSaveErrorSink = (message, cause) => {
	console.error(`[settings] ${message}`, cause);
};

export function installSettingSaveErrorSink(next: SettingSaveErrorSink): void {
	sink = next;
}

export function reportSettingSaveError(message: string, cause: unknown): void {
	sink(message, cause);
}
