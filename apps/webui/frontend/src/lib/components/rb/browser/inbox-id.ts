/** Derived Inbox playlist id (LIBM-D1 / ADR-0019). */
export const INBOX_PLAYLIST_ID = '_incoming';

export function isInboxPlaylistId(id: string | null | undefined): boolean {
	return id === INBOX_PLAYLIST_ID;
}
