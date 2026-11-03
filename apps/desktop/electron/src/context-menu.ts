// The right-click menu in the page: the standard edit menu in text fields.
//
// WKWebView (the Tauri shell) shows Cut, Copy, Paste and Select All on a
// right-click in an editable field, and Copy on selected text. Chromium in
// Electron shows nothing unless the shell builds a menu, so this module does
// (parity matrix row 30). Only roles are used: Chromium performs the action and
// greys out what the focused element cannot do, so the shell adds no logic.

import { Menu, type ContextMenuParams, type MenuItemConstructorOptions, type WebContents } from 'electron';

/** The fields of a `context-menu` event this menu reads. */
export type EditContextParams = Pick<ContextMenuParams, 'isEditable' | 'selectionText' | 'editFlags'>;

/**
 * Cut, Copy, Paste, Select All for an editable field; Copy alone for selected
 * page text; `null` (no menu, as before) anywhere else.
 */
export function editContextMenuTemplate(params: EditContextParams): MenuItemConstructorOptions[] | null {
	const flags = params.editFlags;
	if (params.isEditable) {
		return [
			{ role: 'cut', enabled: flags.canCut },
			{ role: 'copy', enabled: flags.canCopy },
			{ role: 'paste', enabled: flags.canPaste },
			{ type: 'separator' },
			{ role: 'selectAll', enabled: flags.canSelectAll }
		];
	}
	if (params.selectionText.trim() !== '') return [{ role: 'copy', enabled: flags.canCopy }];
	return null;
}

/** Show the edit menu on right-click in `contents`. */
export function installEditContextMenu(contents: WebContents): void {
	contents.on('context-menu', (_event, params) => {
		const template = editContextMenuTemplate(params);
		if (template === null) return;
		Menu.buildFromTemplate(template).popup();
	});
}
