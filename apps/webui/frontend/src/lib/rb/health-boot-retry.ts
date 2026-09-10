/**
 * Boot-time and background health-read retries.
 *
 * Split out of `pane-contract.svelte.ts` (PR #1656 review round 8 fix, then
 * moved here from that file's own inline definition) once adding these two
 * functions there pushed that file past the 600-line file-size gate. Kept as
 * its own small module and re-exported through `pane-contract.svelte.ts` so
 * `BrowserPanel.svelte` - a measured fan-out hotspot (FANOUT-CONVENTIONS.md)
 * - gains no new import edge: it already imports both names from that one
 * barrel file, unchanged.
 */

/** The shape of `$lib/api`'s `getHealth`, taken as a parameter rather than
 * imported so a test can drive a fake implementation directly - the same
 * injection point `resolveBootPlaylist` and `shouldRetryBootPane` in
 * `pane-contract.svelte.ts` use for the sibling boot-pane decisions. */
type GetHealth = (options?: { fresh?: boolean }) => Promise<{
	health: { state_db: { tracks: number } };
	bindWarning: string | null;
}>;

/**
 * getHealth() at boot, with one retry.
 *
 * _init() throws on any getHealth() rejection, which skips
 * _restoreBootPane() entirely - and _refreshLibraryRowsOnce's background
 * refresh loop explicitly skips any pane whose playlist_id is still null,
 * so a boot pane that never opened this way is never retried by anything
 * else. One retry with fresh:true (bypassing the coalesced entry, which
 * may itself be the failed attempt) covers a daemon that is merely slow -
 * including the fetch timeout src/lib/api.ts now adds - rather than
 * actually down. A second failure still propagates to _init()'s existing
 * catch/toast path unchanged.
 */
export async function getHealthAtBoot(getHealth: GetHealth): ReturnType<GetHealth> {
	try {
		return await getHealth();
	} catch {
		return await getHealth({ fresh: true });
	}
}

/**
 * getHealth({ fresh: true }) for a background library refresh, with one
 * retry.
 *
 * Once the bus's first-ever open has fired, nothing else retries this read:
 * the fallback poll in BrowserPanel stands down while the connection is
 * open, and 'initial-connect' fires exactly once. A single transient
 * failure here must not permanently forfeit the one chance to repair a boot
 * pane a stale coalesced snapshot left blank (PR #1656 review round 7, P2
 * BLOCKING).
 */
export async function getHealthFreshWithRetry(getHealth: GetHealth): ReturnType<GetHealth> {
	try {
		return await getHealth({ fresh: true });
	} catch {
		return await getHealth({ fresh: true });
	}
}
