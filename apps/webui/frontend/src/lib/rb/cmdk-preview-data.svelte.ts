/**
 * CMDK-07: what the Cmd K preview strip draws, read without decoding audio.
 *
 * The waveform is the overview strip and the vocal regions the library rows
 * already read from disk (`POST /library/row-assets`, the same answer a
 * library row in view gets). Sections, hot cues and the beat grid come from
 * the shared `/anlz` cache the browser's row-select prefetch fills, through
 * that same prefetch call, so the bar never fetches a payload the browser
 * would not. Nothing is asked for until a row has been rested on (the bar's
 * dwell), so arrowing through a list never fetches every row.
 */
import { SvelteMap } from 'svelte/reactivity';
import { fetchPreviewAsset, type PreviewStripData, type Vocals } from './row-assets-fill';

export type PreviewAssets =
	| { status: 'loading' }
	| { status: 'ready'; strip: PreviewStripData | null; vocals: Vocals }
	| { status: 'error'; message: string };

/** Tracks whose assets are held at once; the oldest goes first. A strip is 360 bytes. */
const HELD = 64;
/** A track still being analyzed is asked for again after this long, at most PENDING_TRIES times. */
export const PENDING_RETRY_MS = 2000;
export const PENDING_TRIES = 15;
const _tries = new Map<string, number>();
/** Tracks still pending when their retries ran out or the highlight left them: the next dwell asks again (Codex r4275809110). */
const _capped = new Set<string>();

const _assets = new SvelteMap<string, PreviewAssets>();
/** The track the bar has rested on now; a pending retry runs only for it (Sol r4278366209). */
let _watched: string | null = null;

/**
 * The bar's dwell names the track it rests on, and null once the highlight
 * leaves it or the bar closes. A still-pending track is asked for again only
 * while it is the watched one; one left behind is asked again on its next dwell.
 */
export function watchPreview(stable_id: string | null): void {
	_watched = stable_id;
}

/** Pure read; undefined until `requestPreviewAssets` has been called for the id. */
export function previewAssets(stable_id: string): PreviewAssets | undefined {
	return _assets.get(stable_id);
}

/**
 * Ask for one track's strip and vocals unless they are held or on the way.
 * Mutates rune state: call from an event or an effect, never a `$derived`.
 */
export function requestPreviewAssets(stable_id: string): void {
	const held = _assets.get(stable_id);
	if (held !== undefined && held.status !== 'error') {
		// Touch it so the most recently previewed tracks are the ones kept.
		_assets.delete(stable_id);
		_assets.set(stable_id, held);
		// Analysis outlived the retries: keep drawing what is held and ask once more on this visit.
		if (_capped.delete(stable_id)) _fetchInto(stable_id);
		return;
	}
	_assets.set(stable_id, { status: 'loading' });
	while (_assets.size > HELD) {
		const oldest = _assets.keys().next().value;
		if (oldest === undefined) break;
		_assets.delete(oldest);
		_capped.delete(oldest);
	}
	_fetchInto(stable_id);
}

/** Fetch into the held entry; a pending answer is drawn as it is and asked for again. */
function _fetchInto(stable_id: string): void {
	fetchPreviewAsset(stable_id).then(
		(asset) => {
			if (!_assets.has(stable_id)) return;
			if (asset === null) {
				_assets.set(stable_id, { status: 'error', message: 'not a library track' });
				return;
			}
			const entry: PreviewAssets = { status: 'ready', strip: asset.strip, vocals: asset.vocals };
			_assets.set(stable_id, entry);
			const tries = (_tries.get(stable_id) ?? 0) + 1;
			if (!asset.pending || tries > PENDING_TRIES) {
				_tries.delete(stable_id);
				if (asset.pending) _capped.add(stable_id);
				return;
			}
			// Still being analyzed: keep showing this answer, then ask again so the strip fills in,
			// only while the bar still rests on it. A track left behind keeps what it drew and is
			// asked again on its next dwell, so nothing polls outside its highlight.
			_tries.set(stable_id, tries);
			setTimeout(() => {
				if (_assets.get(stable_id) !== entry) return;
				if (_watched === stable_id) _fetchInto(stable_id);
				else {
					_tries.delete(stable_id);
					_capped.add(stable_id);
				}
			}, PENDING_RETRY_MS);
		},
		(error: unknown) => {
			if (!_assets.has(stable_id)) return;
			_assets.set(stable_id, { status: 'error', message: error instanceof Error ? error.message : String(error) });
		}
	);
}
