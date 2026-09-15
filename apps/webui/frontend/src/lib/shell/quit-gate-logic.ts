/** Pure quit-request decision logic for INSTALL-21 (unit-testable without IPC). */

export type QuitRequestAction = 'confirm' | 'open-dialog';

export function planQuitRequest(input: {
	force?: boolean;
	dialogOpen: boolean;
	needsConfirmation: boolean;
}): QuitRequestAction {
	if (input.force === true) return 'confirm';
	if (input.dialogOpen) return 'confirm';
	if (!input.needsConfirmation) return 'confirm';
	return 'open-dialog';
}
