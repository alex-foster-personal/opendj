/**
 * Keyboard + pointer wiring for LIBUX-05 "Technically-working mode".
 *
 * cmd+R (Ctrl+R on non-Mac) - press toggles overlay mode on/off; holding it
 * peeks the full UI back for as long as it is held. Cmd+R is also a browser's
 * reload chord: the desktop shell claims it and preventDefaults every branch,
 * while a browser tab leaves Cmd+R to the browser (LIBUX-29, reload-chord.ts)
 * and toggles on Ctrl+R only.
 * Opt/Alt held - reveals the "no obvious home" group (mixer/topbar/quick
 * draw) over a translucent backdrop. Hold-only: there is no toggle reading.
 * cmd+E (Ctrl+E) - toggles the floating EQ panel. Only meaningful in overlay
 * mode; the setter itself is a no-op outside it.
 * Mouse-to-edge - while in overlay mode and not peeking, moving the pointer
 * within EDGE_HOVER_PX of a viewport edge reveals that edge's region.
 *
 * Installed from /performance alongside the other install* hotkey modules
 * (performance-hotkeys.ts, settings/hotkeys.ts) and gated the same way:
 * ignored while the settings overlay is open or the target is a text input.
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { createHoldOrPress } from '$lib/gestures/hold-or-press';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { pageOwnsReloadChord } from '$lib/rb/reload-chord';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { nativeShellKind } from '$lib/shell/native-shell';
import {
	hoveredEdgeList,
	isEqRaised,
	isTechModeActive,
	type EdgeRegion
} from '$lib/rb/technically-working.svelte';

const HOLD_THRESHOLD_MS = 350;
const EDGE_HOVER_PX = 40;

// The selector each edge reveals. Only used to EXTEND an already-hovered
// edge (see _updateEdgeHover) - a hidden region is opacity/pointer-events
// hidden, not display:none, so getBoundingClientRect() still returns its
// full layout rect even while invisible. Using rect-containment to decide
// whether to INITIATE a reveal would let the pointer "discover" a region it
// cannot see yet; it may only keep one open that proximity already opened.
const EDGE_REGION_SELECTOR: Record<EdgeRegion, string> = {
	top: '.rb-wavestack',
	bottom: '.rb-browser',
	left: '.deck-col:first-child',
	right: '.deck-col:last-child'
};

function _typingTarget(t: EventTarget | null): boolean {
	return isTextEntryTarget(t);
}

function _isModChord(e: KeyboardEvent, key: string): boolean {
	const mod = e.metaKey || e.ctrlKey;
	return mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === key;
}

function _isOptOnly(e: KeyboardEvent): boolean {
	return e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey && e.key === 'Alt';
}

function _pointerInRegion(edge: EdgeRegion, x: number, y: number): boolean {
	const el = document.querySelector(EDGE_REGION_SELECTOR[edge]);
	if (!el) return false;
	const r = el.getBoundingClientRect();
	return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
}

function _updateEdgeHover(e: PointerEvent | MouseEvent): void {
	if (!isTechModeActive()) return;
	const w = window.innerWidth;
	const h = window.innerHeight;
	const alreadyHovered = new Set(hoveredEdgeList());
	const edges: Array<[EdgeRegion, boolean]> = [
		['top', e.clientY <= EDGE_HOVER_PX],
		['bottom', e.clientY >= h - EDGE_HOVER_PX],
		['left', e.clientX <= EDGE_HOVER_PX],
		['right', e.clientX >= w - EDGE_HOVER_PX]
	];
	for (const [edge, nearEdge] of edges) {
		const sticky = alreadyHovered.has(edge) && _pointerInRegion(edge, e.clientX, e.clientY);
		void runPerformanceCommandFromUi({
			type: 'tech_mode_edge_hover', edge, hovered: nearEdge || sticky
		});
	}
}

function _clearEdgeHover(): void {
	for (const edge of ['top', 'bottom', 'left', 'right'] as const) {
		void runPerformanceCommandFromUi({ type: 'tech_mode_edge_hover', edge, hovered: false });
	}
}

export function installTechnicallyWorkingHotkeys(): () => void {
	// Routed through runPerformanceCommandFromUi, never the technically-working
	// state setters directly: those setters have no notion of the preset
	// lifecycle lock (_presetClaim in performance-ipc.svelte.ts), so calling
	// them straight from a keyboard handler would let cmd+R/Opt/cmd+E mutate
	// tech-mode state while a preset owns the controls, even though the
	// equivalent agent/browser-driven tech_mode_* command is correctly
	// rejected for the same window.
	const rGesture = createHoldOrPress({
		holdThresholdMs: HOLD_THRESHOLD_MS,
		onPress: () => void runPerformanceCommandFromUi({ type: 'tech_mode_toggle' }),
		onHoldStart: () => void runPerformanceCommandFromUi({ type: 'tech_mode_peek', peeking: true }),
		onHoldEnd: () => void runPerformanceCommandFromUi({ type: 'tech_mode_peek', peeking: false })
	});
	const optGesture = createHoldOrPress({
		holdThresholdMs: 0,
		onHoldStart: () =>
			void runPerformanceCommandFromUi({ type: 'tech_mode_opt_reveal', revealed: true }),
		onHoldEnd: () =>
			void runPerformanceCommandFromUi({ type: 'tech_mode_opt_reveal', revealed: false })
	});

	const onKeyDown = (e: KeyboardEvent): void => {
		if (isSettingsOpen() || _typingTarget(e.target)) return;
		if (_isModChord(e, 'r')) {
			if (!pageOwnsReloadChord(e, nativeShellKind())) return;
			e.preventDefault();
			e.stopPropagation();
			rGesture.keyDown();
		} else if (_isModChord(e, 'e')) {
			e.preventDefault();
			e.stopPropagation();
			if (!e.repeat) {
				void runPerformanceCommandFromUi({ type: 'tech_mode_eq_raised', raised: !isEqRaised() });
			}
		} else if (_isOptOnly(e)) {
			e.preventDefault();
			optGesture.keyDown();
		}
	};

	const onKeyUp = (e: KeyboardEvent): void => {
		if (e.key.toLowerCase() === 'r' && rGesture.down) {
			e.preventDefault();
			rGesture.keyUp();
		} else if (e.key === 'Alt' && optGesture.down) {
			e.preventDefault();
			optGesture.keyUp();
		}
	};

	const onPointerMove = (e: PointerEvent): void => _updateEdgeHover(e);
	const onPointerLeave = (): void => _clearEdgeHover();
	const onBlur = (): void => {
		// A lost window focus (alt-tab, devtools, etc.) cannot deliver the
		// matching keyup. optGesture's threshold is 0, so `down` always means
		// `holding` and keyUp() is always the resolving path. rGesture has a
		// real pending window (350ms) before a hold commits - blurring inside
		// it is not a completed press, so cancel() there instead of keyUp(),
		// or the blur itself would toggle tech mode as a spurious press. Once
		// rGesture.holding is true it already opened the peek state, so that
		// case still resolves through keyUp() or peeking sticks open.
		if (rGesture.down) {
			if (rGesture.holding) rGesture.keyUp();
			else rGesture.cancel();
		}
		if (optGesture.down) optGesture.keyUp();
		_clearEdgeHover();
	};

	window.addEventListener('keydown', onKeyDown);
	window.addEventListener('keyup', onKeyUp);
	window.addEventListener('pointermove', onPointerMove);
	document.addEventListener('pointerleave', onPointerLeave);
	window.addEventListener('blur', onBlur);

	return () => {
		window.removeEventListener('keydown', onKeyDown);
		window.removeEventListener('keyup', onKeyUp);
		window.removeEventListener('pointermove', onPointerMove);
		document.removeEventListener('pointerleave', onPointerLeave);
		window.removeEventListener('blur', onBlur);
	};
}
