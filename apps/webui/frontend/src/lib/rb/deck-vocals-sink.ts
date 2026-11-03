/**
 * The deck side of the authoritative ANLZ vocals refresh (issue #1031): when
 * anlz-vocals-refresh.ts publishes a fresher `vocals` field for a track, every
 * deck holding that stable_id adopts it, except a deck whose own payload is
 * newer than the refresh. Split out of audio-engine.svelte.ts as a pure
 * factory over ports (same pattern as createBeatgridResyncGuards) so the
 * unit tests install the exact sink production installs.
 */
import type { AnlzData } from "$lib/rb/anlz-types";
import type { AnlzWithVocals, Vocals } from "$lib/rb/api-rb";

export interface DeckVocalsSinkPorts<D extends PropertyKey> {
  deckIds: readonly D[];
  deckLoadToken: (deck: D) => number;
  deckStableId: (deck: D) => string | null;
  deckAnlz: (deck: D) => AnlzData | null;
  publishDeckAnlz: (deck: D, anlz: AnlzWithVocals) => void;
}

export interface DeckVocalsSink {
  /** Called before the refresh fetch starts: records each holder's load token. */
  snapshot: (stable_id: string) => void;
  /** `previous` is the shared cache's vocals the refresh replaced (null if none). */
  publish: (
    stable_id: string,
    data: AnlzWithVocals,
    previous: Vocals | null,
  ) => void;
}

function _sameVocals(a: Vocals | undefined, b: Vocals | null): boolean {
  if (a === undefined || b === null) return false;
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

export function createDeckVocalsSink<D extends PropertyKey>(
  ports: DeckVocalsSinkPorts<D>,
): DeckVocalsSink {
  const tokensAtStart = new Map<string, Map<D, number>>();
  return {
    snapshot(stable_id) {
      const tokens = new Map<D, number>();
      for (const deck of ports.deckIds) {
        if (ports.deckStableId(deck) === stable_id)
          tokens.set(deck, ports.deckLoadToken(deck));
      }
      tokensAtStart.set(stable_id, tokens);
    },
    publish(stable_id, data, previous) {
      const tokens = tokensAtStart.get(stable_id);
      tokensAtStart.delete(stable_id);
      for (const deck of ports.deckIds) {
        const anlz = ports.deckAnlz(deck) as AnlzWithVocals | null;
        if (ports.deckStableId(deck) !== stable_id || anlz === null) continue;
        const unchangedSinceStart =
          tokens?.get(deck) === ports.deckLoadToken(deck);
        // A holder (re)loaded while the request was in flight adopts the refresh
        // only when it still carries the vocals the refresh replaced; a payload
        // that differs is at least as new as the refresh and stays.
        if (!unchangedSinceStart && !_sameVocals(anlz.vocals, previous))
          continue;
        ports.publishDeckAnlz(deck, { ...anlz, vocals: data.vocals });
      }
    },
  };
}
