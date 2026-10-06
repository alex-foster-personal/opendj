/**
 * Fill the Preview strips of rows in view without a click (NATIVE-21).
 *
 * A long-lived page keeps each row's strip as it was LISTED. Strips written
 * since (the ahead-analysis drain, a mirrored ANLZ) only reached a row when it
 * was clicked. This asks `POST /library/preview-strips` for the strip-less ids
 * in view (plus margin), debounced, one batch at a time, never one request
 * per row and never the same id twice at once. An id the server reports
 * `pending` (the drain was bumped for it) is asked again with backoff while it
 * stays in view; a filled or not-pending id is left alone until it scrolls
 * away and back.
 *
 * Requirements (mini-PRD):
 *   ✔︎ rows in view fill without a click
 *     [if] a visible row has no strip [then] it is in the next batch
 *     [if] an id is already in flight [then] it is not asked again
 *   ✔︎ pending ids back off, then stop
 *     [if] an id stays pending [then] it is re-asked after 2 s, 4 s, ... 15 s
 *     [if] an id scrolls away [then] it is no longer asked
 */

import type { RowAssetTarget } from './row-assets-fill';
import { bootScheduler, type BootScheduler } from './boot-scheduler';

export const STRIP_FILL_DEBOUNCE_MS = 150;
export const STRIP_FILL_MAX_IDS = 200;
export const STRIP_FILL_FIRST_RETRY_MS = 2000;
export const STRIP_FILL_MAX_RETRY_MS = 15000;

export interface StripWire {
  preview_b64: string;
  preview_max: number;
}

export interface PreviewStripBatch {
  strips: Record<string, StripWire | null>;
  pending: string[];
}

export interface StripFillState {
  /** Pending answers so far; drives the backoff. */
  attempts: number;
  /** Earliest time (ms) the id may be asked again. */
  dueAt: number;
  /** Filled, or null and not pending: never asked again while in view. */
  settled: boolean;
}

export interface StripFillDeps {
  fetchBatch(ids: string[]): Promise<PreviewStripBatch>;
  onStrip(stable_id: string, strip: StripWire): void;
  onError(error: unknown): void;
  now(): number;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

/** Backoff before re-asking a pending id: 2 s, 4 s, 8 s, then 15 s. */
export function stripRetryDelayMs(attempts: number): number {
  return Math.min(
    STRIP_FILL_MAX_RETRY_MS,
    STRIP_FILL_FIRST_RETRY_MS * 2 ** Math.max(0, attempts - 1),
  );
}

/** Pure: the visible ids to ask for now, in view order, capped at one batch. */
export function idsDueForStrip(
  visible: readonly string[],
  state: ReadonlyMap<string, StripFillState>,
  inFlight: ReadonlySet<string>,
  now: number,
): string[] {
  const out: string[] = [];
  for (const id of new Set(visible)) {
    if (out.length === STRIP_FILL_MAX_IDS) break;
    if (inFlight.has(id)) continue;
    const s = state.get(id);
    if (s === undefined || (!s.settled && s.dueAt <= now)) out.push(id);
  }
  return out;
}

/** Pure: an id's state after one answer (or a failed request, `answer` undefined). */
export function nextStripFillState(
  prev: StripFillState | undefined,
  answer: { filled: boolean; pending: boolean } | undefined,
  now: number,
): StripFillState {
  const attempts = (prev?.attempts ?? 0) + 1;
  if (answer !== undefined && (answer.filled || !answer.pending)) {
    return { attempts, dueAt: Number.POSITIVE_INFINITY, settled: true };
  }
  return { attempts, dueAt: now + stripRetryDelayMs(attempts), settled: false };
}

export class PreviewStripFiller {
  readonly #deps: StripFillDeps;
  #visible: string[] = [];
  readonly #state = new Map<string, StripFillState>();
  readonly #inFlight = new Set<string>();
  #timer: unknown = null;
  #disposed = false;

  constructor(deps: StripFillDeps) {
    this.#deps = deps;
  }

  /** The strip-less ids in view (plus margin). Debounced. */
  setVisible(ids: readonly string[]): void {
    this.#visible = [...new Set(ids)];
    const keep = new Set(this.#visible);
    for (const id of [...this.#state.keys()]) {
      if (!keep.has(id)) this.#state.delete(id);
    }
    this.#schedule(STRIP_FILL_DEBOUNCE_MS);
  }

  dispose(): void {
    this.#disposed = true;
    if (this.#timer !== null) this.#deps.clearTimer(this.#timer);
    this.#timer = null;
  }

  #schedule(ms: number): void {
    if (this.#disposed) return;
    if (this.#timer !== null) this.#deps.clearTimer(this.#timer);
    this.#timer = this.#deps.setTimer(() => {
      this.#timer = null;
      void this.#run();
    }, ms);
  }

  async #run(): Promise<void> {
    const ids = idsDueForStrip(
      this.#visible,
      this.#state,
      this.#inFlight,
      this.#deps.now(),
    );
    if (ids.length > 0) {
      for (const id of ids) this.#inFlight.add(id);
      let batch: PreviewStripBatch | undefined;
      try {
        batch = await this.#deps.fetchBatch(ids);
      } catch (error) {
        this.#deps.onError(error);
      } finally {
        for (const id of ids) this.#inFlight.delete(id);
      }
      if (this.#disposed) return;
      const now = this.#deps.now();
      const pending = new Set(batch?.pending ?? []);
      const visible = new Set(this.#visible);
      for (const id of ids) {
        const strip = batch?.strips[id] ?? null;
        if (strip !== null) this.#deps.onStrip(id, strip);
        if (!visible.has(id)) continue;
        const answer =
          batch === undefined
            ? undefined
            : { filled: strip !== null, pending: pending.has(id) };
        this.#state.set(
          id,
          nextStripFillState(this.#state.get(id), answer, now),
        );
      }
    }
    this.#scheduleNextDue();
  }

  #scheduleNextDue(): void {
    let due = Number.POSITIVE_INFINITY;
    for (const id of this.#visible) {
      const s = this.#state.get(id);
      if (s === undefined && !this.#inFlight.has(id)) due = Math.min(due, 0);
      else if (s !== undefined && !s.settled) due = Math.min(due, s.dueAt);
    }
    if (due === Number.POSITIVE_INFINITY) return;
    this.#schedule(Math.max(0, due - this.#deps.now()));
  }
}

/**
 * The ids the filler may ask for: rows in view plus `margin` rows each side that
 * still have no strip by any route (LIBM-172). Since the library index carries no
 * strips, this window is the ONLY thing that keeps a 9,713-row list from asking
 * for every row's disk reads at once.
 */
export function stripLessIdsNear(
  rows: readonly { stable_id: string; strip: unknown }[],
  startIndex: number,
  endIndex: number,
  margin: number,
  hasStrip: (stable_id: string) => boolean,
): string[] {
  return rows
    .slice(Math.max(0, startIndex - margin), endIndex + margin)
    .filter((r) => r.strip === null && !hasStrip(r.stable_id))
    .map((r) => r.stable_id);
}

/** The filler's batch for library rows (LIBM-172): `POST /library/row-assets`, with the
 * module that applies it loaded on first use so it stays off the eager route bundle. */
export async function fetchRowAssetsLazily(
  ids: string[],
  rows: readonly RowAssetTarget[],
): Promise<PreviewStripBatch> {
  return (await import('./row-assets-fill')).fetchAndApplyRowAssets(ids, rows);
}

/**
 * Cover images wait in the boot scheduler's deferred queue, the one hold every
 * non-critical boot read uses (LIBM-166, LIBM-172), so ~30 artwork reads do not
 * compete with the boot library index on the single-worker engine. Returns whether
 * they may load now; otherwise calls `release` once, when the queue drains. The
 * queue's own ceiling means it never strands them.
 */
export function holdArtworkForBoot(
  release: () => void,
  scheduler: Pick<BootScheduler, 'defer'> = bootScheduler,
): boolean {
  let returned = false;
  let ranAtOnce = false;
  scheduler.defer('track-table:artwork', () => {
    if (returned) release();
    else ranAtOnce = true;
  });
  returned = true;
  return ranAtOnce;
}
