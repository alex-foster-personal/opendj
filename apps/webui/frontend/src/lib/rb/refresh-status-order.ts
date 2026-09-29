/**
 * Which polled refresh status the button may show (issue #3886).
 *
 * Status answers can arrive out of order. A GET answered before the start
 * POST would put the idle status back and the run would stop being followed
 * (Codex P2 4130868861); two overlapping polls could apply a run's end and
 * then an earlier running snapshot of it, restarting the polling and toasting
 * twice (4131292228). The engine's own stamps order every snapshot: a run's
 * end (finished_at) comes after its start, and the single job slot starts a
 * run only after the last one ended, so `finished_at ?? started_at` never
 * goes back. A null stamp (no run, or nothing shown yet) compares as 0.
 *
 * The one snapshot the stamps cannot order is the no-job idle one (both
 * stamps null): stale when it was asked for before the shown snapshot
 * arrived (the GET that raced the POST), but server truth when asked for
 * after it, because only a restarted backend forgets its job. Refusing that
 * one kept the button spinning and polling forever (Codex P2 4132045411).
 */
import type { RefreshStatus } from './api-ingest';

type Stamps = Pick<RefreshStatus, 'started_at' | 'finished_at'>;

// The `!` are deliberate: `<` reads a null stamp as 0, and nothing is older.
export function refreshSnapshotApplies(shown: Stamps | null, next: Stamps, askedAfterShown: boolean): boolean {
	const at = (x: Stamps | null) => x && (x.finished_at ?? x.started_at);
	return (at(next) === null && askedAfterShown) || !(at(next)! < at(shown)!);
}
