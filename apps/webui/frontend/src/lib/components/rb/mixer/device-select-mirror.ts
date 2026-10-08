/**
 * IOPIN-16: a device <select> shows what the router HOLDS, never only what was
 * clicked. A pick can be refused (the sink failed and the route rolled back),
 * redirected (CUEOUT-26: a CUE on MAIN's own output becomes split L/R with no
 * separate CUE) or be a no-op. In each case the bound state may not change, so
 * Svelte never rewrites the element and it keeps showing the clicked device:
 * "External Headphones" under HEADPHONE CUE while no CUE is selected (the maintainer,
 * Thu 8 Oct 2026). Once the pick settles, the element is set from the state.
 */
export type DevicePick = (deviceId: string) => void | Promise<unknown>;

export async function pickThenMirror(
	select: { value: string },
	pick: DevicePick,
	held: () => string | null
): Promise<void> {
	try {
		await pick(select.value);
	} finally {
		select.value = held() ?? '';
	}
}
