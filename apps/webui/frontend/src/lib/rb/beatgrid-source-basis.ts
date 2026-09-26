/**
 * One definition of "does this /anlz payload's beatgrid source agree with a
 * lane-wide rbx-vs-own selection", for every client check that rejects or
 * evicts a payload fetched under a different selection (PARITY-02).
 *
 * The lane-wide selection (`GET /api/v1/analysis/source`) is no longer the
 * source of every track: STANDALONE-06 (issue #3536) serves an UNMAPPED track
 * from `own` while the lane default is still `rbx` and the toggle is unset.
 * Comparing such a payload's `beatgrid_source` against the lane-wide value
 * read every unmapped track as a straggler from a switch, and
 * `fetchAnlzUntilSourceConfirmed` then refetched it forever: about 3,900
 * `/anlz` requests in 45 s and a deck that never finished loading. The engine
 * now stamps `beatgrid_source_basis`, and only an explicit
 * 'unmapped-default' is allowed to differ, and only from a `rekordbox`
 * selection, the one state STANDALONE-06 applies in. A payload with no basis
 * keeps the old, stricter comparison.
 *
 * Pure, no runes, so it is unit-testable in isolation.
 */
import type { AnlzBeatgridSource, AnlzData } from './anlz-types';

type SourceStamped = Pick<AnlzData, 'beatgrid_source' | 'beatgrid_source_basis'>;

/** True when `data` is what the engine serves this track under the lane-wide
 * `selected` source. */
export function anlzSourceMatchesSelection(data: SourceStamped, selected: AnlzBeatgridSource): boolean {
	if (data.beatgrid_source === selected) return true;
	return selected === 'rekordbox' && isUnmappedDefault(data);
}

/** True when `data`'s source came from STANDALONE-06's per-track default
 * rather than the lane-wide selection, so it says nothing about which
 * selection was in force when it was served. */
export function isUnmappedDefault(data: SourceStamped): boolean {
	return data.beatgrid_source === 'own' && data.beatgrid_source_basis === 'unmapped-default';
}
