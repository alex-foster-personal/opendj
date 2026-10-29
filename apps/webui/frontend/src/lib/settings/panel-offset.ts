/**
 * Where the settings panel may sit after a move. One rule shared by the header
 * drag and the `window.__mdtSettingsPanel` agent bridge, so an agent cannot
 * park the panel somewhere a pointer could not (Sol P1, PR #6829).
 */

/** Pixels of the panel that always stay on screen, so its header can be grabbed back. */
export const PANEL_KEEP_PX = 64;

export interface PanelOffset {
	dx: number;
	dy: number;
}

/**
 * Clamp a requested offset from the panel's centered slot. `baseLeft` and
 * `baseTop` are where the panel sits at offset zero; `width` is its width.
 * The panel may slide off the left, right and bottom edges until only
 * {@link PANEL_KEEP_PX} remains, and never above the top of the window.
 */
export function clampPanelOffset(
	want: PanelOffset,
	base: { left: number; top: number; width: number },
	viewport: { width: number; height: number }
): PanelOffset {
	if (!Number.isFinite(want.dx) || !Number.isFinite(want.dy)) {
		throw new RangeError(`settings panel offset must be finite, got ${want.dx}, ${want.dy}`);
	}
	return {
		dx: Math.min(viewport.width - PANEL_KEEP_PX - base.left, Math.max(PANEL_KEEP_PX - base.width - base.left, want.dx)),
		dy: Math.min(viewport.height - PANEL_KEEP_PX - base.top, Math.max(-base.top, want.dy))
	};
}

/** Agent parity for the header drag: the same move and reset, same bounds. */
export interface SettingsPanelBridge {
	/** Current offset from the centered slot, px; zero while the panel is closed. */
	offset: () => PanelOffset;
	/** Move by a delta, clamped like a drag; throws while the panel is closed. Returns the new offset. */
	moveBy: (dx: number, dy: number) => PanelOffset;
	/** Back to the centered slot; throws while the panel is closed. */
	reset: () => PanelOffset;
}

/** What an open panel lends the bridge: its offset and its one placement path. */
export interface AttachedSettingsPanel {
	offset: () => PanelOffset;
	place: (want: PanelOffset) => PanelOffset;
}

let _attached: AttachedSettingsPanel | null = null;

/** Called by the open panel; the returned function detaches it again. */
export function attachSettingsPanel(panel: AttachedSettingsPanel): () => void {
	_attached = panel;
	return () => {
		if (_attached === panel) _attached = null;
	};
}

function _openPanel(): AttachedSettingsPanel {
	if (_attached === null) throw new Error('settings panel is not open');
	return _attached;
}

/**
 * Page-lifetime bridge. It outlives the lazily mounted panel component, so a
 * call while settings are closed reaches it and gets the explicit closed-panel
 * error rather than an undefined property (Sol P1, PR #6829).
 */
export const settingsPanelBridge: SettingsPanelBridge = {
	offset: () => (_attached === null ? { dx: 0, dy: 0 } : _attached.offset()),
	moveBy: (dx, dy) => {
		const panel = _openPanel();
		const at = panel.offset();
		return panel.place({ dx: at.dx + dx, dy: at.dy + dy });
	},
	reset: () => _openPanel().place({ dx: 0, dy: 0 })
};

if (typeof window !== 'undefined') {
	(window as Window & { __mdtSettingsPanel?: SettingsPanelBridge }).__mdtSettingsPanel = settingsPanelBridge;
}
