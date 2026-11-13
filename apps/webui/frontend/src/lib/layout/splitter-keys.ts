// WAI-ARIA Window Splitter key handling (PLAN.md section 2.3) as a pure step.
// Returns the next state, or null when the key is not consumed so that
// performance hotkeys (letters, Space, Tab) keep working.

export const SPLITTER_STEP_PX = 8;
export const SPLITTER_SHIFT_STEP_PX = 64;

export interface SplitterKeyMods {
	shift?: boolean;
}

export interface SplitterKeyState {
	px: number;
	min: number;
	max: number;
	collapsed: boolean;
	collapsible: boolean;
}

export interface SplitterKeyResult {
	px: number;
	collapsed: boolean;
}

function clamp(px: number, min: number, max: number): number {
	return Math.min(max, Math.max(min, px));
}

export function splitterKeyStep(
	key: string,
	mods: SplitterKeyMods,
	state: SplitterKeyState
): SplitterKeyResult | null {
	const step = mods.shift ? SPLITTER_SHIFT_STEP_PX : SPLITTER_STEP_PX;
	const { px, min, max, collapsed } = state;
	switch (key) {
		case 'ArrowRight':
		case 'ArrowDown':
			return { px: clamp(px + step, min, max), collapsed };
		case 'ArrowLeft':
		case 'ArrowUp':
			return { px: clamp(px - step, min, max), collapsed };
		case 'Home':
			return { px: min, collapsed };
		case 'End':
			return { px: max, collapsed };
		case 'Enter':
			return state.collapsible ? { px, collapsed: !collapsed } : null;
		default:
			return null;
	}
}
