/** Reactive change signal for the persisted MIDI choice (midi-enabled-choice).
 * A rune module, because $state may only be declared in .svelte(.ts) files;
 * the leaf stays plain TypeScript so its runtime-free contract holds. */
export const midiEnabledTick = $state({ n: 0 });

// Written from a plain counter so a bump never READS the state: a write made
// from inside an effect must not make that effect depend on the tick.
let _count = 0;

export function bumpMidiEnabledTick(): void {
	_count += 1;
	midiEnabledTick.n = _count;
}
