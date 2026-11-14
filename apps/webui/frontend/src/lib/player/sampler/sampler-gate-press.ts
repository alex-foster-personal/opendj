/**
 * Edge sampler: a gate tab's play control as a press (#10782 follow-up of #12169, spec 4.3 gate).
 * Pointer down fires the slot; the first let-go (pointer up, pointer cancel or lost capture)
 * releases the SAME slot once, even if the tab flipped to another set while it was held.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 one press fires once and releases once, the slot it pressed
 *     [if] up, cancel and lost capture all follow one down [then] release runs once [else ⛔️]
 *     [if] the tab's slot changes while held [then] the pressed slot is released [else ⛔️]
 */

/** Press handlers for a gate slot's play control: `down` fires, `up` releases what `down` fired. */
export function createGatePress<S extends { slot_id: string }>(
	fire: (slot: S) => void,
	release: (slotId: string) => void
): { down(slot: S): void; up(): void } {
	let held: string | null = null;
	return {
		down(slot) {
			if (held !== null) return;
			held = slot.slot_id;
			fire(slot);
		},
		up() {
			if (held === null) return;
			const id = held;
			held = null;
			release(id);
		}
	};
}
