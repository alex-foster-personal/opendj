/** Shared CloudSync chip state for TopBar clock visibility (CHROME-06). */
import type { CloudSyncStatus } from '$lib/api-cloudsync';
import { chipState as chipStateOf, type ChipState } from '$lib/components/cloudsync/cloudsync-view';

export const cloudSyncChipState: { value: ChipState } = $state({ value: 'off' });

export function publishCloudSyncChipState(status: CloudSyncStatus | null): void {
	cloudSyncChipState.value = chipStateOf(status);
}
