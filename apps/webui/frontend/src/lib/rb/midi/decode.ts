/**
 * Pure wire decoding for the MIDI runtime (build unit: midi core).
 *
 * Split out of webmidi.svelte.ts when that file crossed the 600-line limit:
 * everything here is a total function of its arguments, with no WebMIDI
 * access, no rune state and no I/O, so it is testable on its own and safe to
 * import from anywhere. webmidi.svelte.ts re-exports the three public decoders
 * so existing callers and tests keep their import path.
 *
 * Grounding for the encoder ranges is spike 2a, cited per function.
 *
 * Requirements (mini-PRD):
 *   ✔︎ decodeSource returns null for families outside P0 scope rather than
 *     inventing a source. [if] a program-change byte decodes [then ⛔️] broken
 *   ✔︎ decodeRelative reads two's-complement ticks, so 0x7F is -1 not 127.
 *   ✔︎ bindingKey separates the shift layer, so a shifted twin never collides
 *     with its unshifted binding.
 */

import type { MidiSource } from '$lib/rb/midi/midi-types';

/** Decode a raw status byte triple into a MidiSource, or null for families
 * outside P0 scope (aftertouch, program change, realtime...). */
export function decodeSource(status: number, data1: number): MidiSource | null {
	const family = status & 0xf0;
	const ch = (status & 0x0f) + 1;
	if (family === 0x90 || family === 0x80) return { ch, kind: 'note', id: data1 };
	if (family === 0xb0) return { ch, kind: 'cc', id: data1 };
	if (family === 0xe0) return { ch, kind: 'pitchbend', id: 0 };
	return null;
}

/** Two's-complement relative-encoder decode (spike 2a: CW ticks 0x01..0x1E,
 * CCW ticks 0x7F..0x62 -> signed delta). */
export function decodeRelative(raw: number): number {
	return raw <= 63 ? raw : raw - 128;
}

/** Combine a stored MSB with an arriving LSB into a 14-bit raw value. */
export function combine14(msb: number, lsb: number): number {
	return (msb << 7) | lsb;
}

/** Binding lookup key. The shift layer is part of the key, so a hardware
 * shift twin can never collide with the binding it shadows. */
export function bindingKey(shift: boolean, src: MidiSource): string {
	return `${shift ? 1 : 0}|${src.ch}|${src.kind}|${src.id}`;
}

/** Channel-scoped key for MSB/LSB pairing and the outbound queue. */
export function chKey(ch: number, id: number): string {
	return `${ch}:${id}`;
}
