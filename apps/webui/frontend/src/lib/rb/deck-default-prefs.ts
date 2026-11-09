/**
 * "Quantize on new decks" (SETSTUB-02): the Quantize state a deck starts with
 * when the page opens or the deck is ejected. Default ON, the value every deck
 * was hardcoded to before this pref existed.
 *
 * Disk-backed (ui-prefs `deck_default_quantize`), mirrored in the rb-ui-prefs
 * localStorage blob so the four decks created at module load already see it.
 *
 * Leaf module on purpose: prefs.svelte.ts announces the stored value at load
 * and prefs-hydrate.ts and the setter announce every change here, while
 * player/state.svelte.ts reads `currentDeckDefaults()` and listens. So the deck
 * store never imports prefs.svelte.ts (a hub the quality ratchet caps by fan-in)
 * and neither prefs module imports the deck store. Either load order works: a
 * deck seeded before the announce is still empty and follows it.
 */

export interface DeckDefaultPrefs {
	deck_default_quantize: boolean;
	/** DECKCTL-10421: Needle Lock, a playing deck's overview strip ignores clicks. Default off. */
	deck_needle_lock: boolean;
}

export const DECK_DEFAULT_PREF_DEFAULTS: DeckDefaultPrefs = {
	deck_default_quantize: true,
	deck_needle_lock: false
};

export function mergeDeckDefaultPrefsFromParsed(
	parsed: Partial<Record<keyof DeckDefaultPrefs, unknown>>,
	storageKey: string
): DeckDefaultPrefs {
	const out = { ...DECK_DEFAULT_PREF_DEFAULTS };
	for (const key of Object.keys(DECK_DEFAULT_PREF_DEFAULTS) as (keyof DeckDefaultPrefs)[]) {
		const value = parsed[key];
		if (value === undefined) continue;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
		out[key] = value;
	}
	return out;
}

type DeckDefaultsListener = (prefs: Readonly<DeckDefaultPrefs>) => void;

const _listeners = new Set<DeckDefaultsListener>();
let _current: DeckDefaultPrefs = { ...DECK_DEFAULT_PREF_DEFAULTS };

/** The last announced deck defaults (the built-in default before any announce). */
export function currentDeckDefaults(): Readonly<DeckDefaultPrefs> {
	return _current;
}

/** Subscribe to deck-default changes (setter and disk hydrate); returns unsubscribe. */
export function onDeckDefaultsChange(listener: DeckDefaultsListener): () => void {
	_listeners.add(listener);
	return () => {
		_listeners.delete(listener);
	};
}

export function announceDeckDefaults(prefs: Readonly<DeckDefaultPrefs>): void {
	_current = {
		deck_default_quantize: prefs.deck_default_quantize,
		deck_needle_lock: prefs.deck_needle_lock
	};
	for (const listener of _listeners) listener(prefs);
}

export function makeDeckDefaultPrefSetters(
	state: DeckDefaultPrefs,
	persist: () => void,
	sync: (patch: Partial<DeckDefaultPrefs>) => void
): { setDeckDefaultQuantize(next: boolean): void; setDeckNeedleLock(next: boolean): void } {
	return {
		setDeckNeedleLock(next: boolean): void {
			if (typeof next !== 'boolean') {
				throw new Error(`deck_needle_lock must be a boolean, got ${String(next)}`);
			}
			state.deck_needle_lock = next;
			persist();
			void sync({ deck_needle_lock: next });
			announceDeckDefaults(state);
		},
		setDeckDefaultQuantize(next: boolean): void {
			if (typeof next !== 'boolean') {
				throw new Error(`deck_default_quantize must be a boolean, got ${String(next)}`);
			}
			state.deck_default_quantize = next;
			persist();
			void sync({ deck_default_quantize: next });
			announceDeckDefaults(state);
		}
	};
}
