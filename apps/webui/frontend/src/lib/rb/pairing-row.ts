/** PAIR-04 library chrome: which rows get the purple pairing underline. */

/** True when `stableId` is a pairing partner of the current master. TrackTable
 * binds this to the row's `rb-row-paired` class, fed by PairingIndex.partnerIds. */
export function isPairedRow(partnerIds: ReadonlySet<string>, stableId: string): boolean {
	return partnerIds.has(stableId);
}
