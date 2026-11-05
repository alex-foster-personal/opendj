/** Lazy loader so key-segment playhead math stays out of the performance static closure. */
import { effectiveCamelotKey, keySyncKey, keySyncKeyIsGuess } from '$lib/player/key/camelot';
import type { AnlzData } from '$lib/rb/anlz-types';
import type { DeckKeyFields, KeyAtPlayhead } from '$lib/player/key-playhead';

type KeyFn = (
	anlz: AnlzData | null,
	positionMs: number,
	keyShiftSemitones: number,
	fallbackKey: string | null
) => KeyAtPlayhead;

let impl = $state<KeyFn | null>(null);

void import('$lib/player/key-playhead').then((mod) => {
	impl = mod.keyAtPlayhead;
});

function _fallback(
	keyShiftSemitones: number,
	fallbackKey: string | null
): KeyAtPlayhead {
	const display = effectiveCamelotKey(fallbackKey, keyShiftSemitones) ?? fallbackKey;
	return { display, title: null, markerTimesS: [] };
}

export function keyAtPlayheadNow(
	anlz: AnlzData | null,
	positionMs: number,
	keyShiftSemitones: number,
	fallbackKey: string | null
): KeyAtPlayhead {
	if (impl === null) return _fallback(keyShiftSemitones, fallbackKey);
	return impl(anlz, positionMs, keyShiftSemitones, fallbackKey);
}

/** DECK-KEY-01: `deckKeyAtPlayhead` through the lazy loader (same resolved key, "?" flag). */
export function deckKeyAtPlayheadNow(deck: DeckKeyFields): KeyAtPlayhead & { isGuess: boolean } {
	const at = keyAtPlayheadNow(deck.anlz, deck.position_ms, deck.key_shift_semitones, keySyncKey(deck));
	return { ...at, isGuess: keySyncKeyIsGuess(deck) };
}
