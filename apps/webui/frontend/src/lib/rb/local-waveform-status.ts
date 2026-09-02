/**
 * Pure helper surfacing a PERMANENT local-decode failure (issue #735
 * follow-up, discussion_r3908337231). No runes, no DOM - unit-testable in
 * isolation, mirroring beatgrid-fallback.ts's shape.
 *
 * `local_waveform` is never consumed by deck rendering (WaveRow/render.ts
 * read `waveform`, which stays empty on a decode failure) - without this,
 * a permanently undecodable unmapped track's deck lane renders blank with
 * no error label, because the HTTP response is a normal 200.
 */
import type { AnlzData } from './anlz-types';

/** The stated reason once a local decode has permanently failed (no
 * ffmpeg, corrupt audio, missing file) - never for a transient/retryable
 * condition, which should keep showing whatever state preceded it rather
 * than a misleading terminal-looking label. */
export function localDecodeFailureReason(anlzData: AnlzData | null): string | null {
	const local = anlzData?.local_waveform;
	if (local === undefined || local.status !== 'not_decoded' || local.retryable) return null;
	return local.reason;
}
