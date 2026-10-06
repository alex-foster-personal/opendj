/** SET-12 v1 fallback: the build can switch set recording off (`sets.recording`).
 *  Hidden only when the daemon DECLARES it off; while flags are unread the
 *  control shows and the start route refuses with the reason (403). */
import { buildFlags } from '$lib/api/store-build.svelte';

export const SET_RECORDING_FLAG_ID = 'sets.recording';

export function recordingEnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return flags.find((flag) => flag.flag_id === SET_RECORDING_FLAG_ID)?.enabled !== false;
}

export function setRecordingEnabled(): boolean {
	void buildFlags.load();
	return recordingEnabledIn(buildFlags.flags);
}
