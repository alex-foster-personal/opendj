/**
 * Play from USB (specs/usb-play-from-stick.md section 4b, "Routing"): one
 * place that knows a track can come from a USB stick instead of the library.
 *
 * A stick track id is `usb-{VolumeUUID}-{pdb_track_id}` (USBPLAY-04). Library
 * stable ids are 40-char lowercase sha1 hex digests: every tier of
 * `apps/shared/state/ids.py::stable_id` returns `hashlib.sha1(...).hexdigest()`,
 * and 'u', 's' and '-' are not hex digits, so no library id can carry the
 * prefix and the prefix alone is a sound discriminator.
 *
 * The existing per-track builders call `trackApiPath` so a stick id reaches
 * `/api/v1/usb/tracks/{id}...` (same response models as the library routes)
 * while library ids keep `/api/v1/tracks/{id}...`. Builders with no stick
 * route either skip the request or throw `UsbTrackRefusal` before it.
 *
 * Kept tiny on purpose: it rides in the first-paint chunk with api-rb.ts.
 */
import { RbApiError } from './api-rb-error';

/** A stick track's session edits (hot cues, rating; decision 2) load on
 * demand: every caller already knows the id is a stick id and is async, and a
 * boot that never plays from USB never needs them. Module-cached, so every
 * caller shares the one session store. */
export function loadStickSessionEdits(): Promise<typeof import('./stick-session-edits')> {
	return import('./stick-session-edits');
}

export function isUsbTrackId(id: string): boolean {
	return id.startsWith('usb-');
}

/** The API path for one track's resource, `suffix` included verbatim
 * (for example `/audio` or `/anlz?points=...`). */
export function trackApiPath(id: string, suffix = ''): string {
	return `${isUsbTrackId(id) ? '/api/v1/usb/tracks/' : '/api/v1/tracks/'}${encodeURIComponent(id)}${suffix}`;
}

/** Whether a deck holding `stableId` takes hot cue edits. A library track
 * needs a rekordbox mapping for them to land (#736); a stick track keeps them
 * in the session (spec 4b, decision 2); an empty deck takes none (#804). The
 * one definition HotCueBank's pads and the hot_cue_save IPC gate share. */
export function hotCueEditsAllowed(stableId: string | null, hasRbMapping: boolean): boolean {
	return stableId !== null && (hasRbMapping || isUsbTrackId(stableId));
}

/** Stick ids never enter library-keyed requests (spec 4b: the copilot 404s
 * on ids it does not know). */
export function withoutUsbTrackIds(ids: readonly string[]): string[] {
	return ids.filter((id) => !isUsbTrackId(id));
}

/**
 * Raised for a stick id BEFORE any request is made, so `status` is 0 (no
 * HTTP exchange happened). `USB_READ_ONLY` refuses a write (spec decision 2:
 * nothing is ever written for a stick track); `USB_NOT_SUPPORTED` refuses a
 * library-only read that has no stick route. It extends RbApiError so every
 * consumer that already settles a typed backend refusal (auto-cue cache,
 * stem waveform cache, row hydration) settles this one the same way instead
 * of treating it as a transport failure.
 */
export class UsbTrackRefusal extends RbApiError {
	constructor(code: 'USB_READ_ONLY' | 'USB_NOT_SUPPORTED', stableId: string, action: string) {
		super(0, code, `${action} refused for stick track ${stableId}`);
		this.name = 'UsbTrackRefusal';
	}
}

/** First line of every per-track WRITE builder: throws `USB_READ_ONLY` for a
 * stick id before any request. One call per site instead of an inline
 * if/throw keeps the guard's bytes out of every chunk that carries a builder
 * (the /performance budget is nearly full). */
export function refuseStickWrite(id: string, action: string): void {
	if (isUsbTrackId(id)) throw new UsbTrackRefusal('USB_READ_ONLY', id, action);
}

/** First line of every library-only READ builder (no stick route exists):
 * throws `USB_NOT_SUPPORTED` for a stick id before any request. */
export function refuseStickRead(id: string, action: string): void {
	if (isUsbTrackId(id)) throw new UsbTrackRefusal('USB_NOT_SUPPORTED', id, action);
}
