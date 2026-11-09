/**
 * SETNOTE-6340b: a lone TAP of the right Cmd (or right Ctrl) key.
 *
 * Arms on keydown of a right-hand modifier while no other key is down, fires
 * on its keyup. Any other keydown in between is a chord (right-Cmd+C) and
 * disarms, so copy/paste with the right hand never toggles the pad. The left
 * modifiers never arm: they are the ones every chord in the app is typed with.
 * A left+right pair of the same modifier (MetaLeft+MetaRight) is a chord too
 * (SETNOTE-9940): holding left Cmd and tapping right Cmd never toggles the pad.
 */

export const RIGHT_CMD_CODE = 'MetaRight';
export const RIGHT_CTRL_CODE = 'ControlRight';

function _isTapCode(code: string): boolean {
	return code === RIGHT_CMD_CODE || code === RIGHT_CTRL_CODE;
}

export interface TapKeyEvent {
	code: string;
	repeat?: boolean;
}

export interface RightCmdTapDetector {
	keyDown(e: TapKeyEvent): void;
	/** Returns true when this keyup completes a tap. */
	keyUp(e: TapKeyEvent): boolean;
	blur(): void;
}

export function createRightCmdTapDetector(): RightCmdTapDetector {
	const down = new Set<string>();
	let armed: string | null = null;
	return {
		keyDown(e) {
			if (e.repeat) return;
			if (armed !== null && e.code !== armed) armed = null;
			else if (_isTapCode(e.code) && down.size === 0) armed = e.code;
			down.add(e.code);
		},
		keyUp(e) {
			// macOS delivers no keyup for keys released while Cmd is held, so
			// anything still recorded as down is stale once Cmd lets go.
			if (e.code.startsWith('Meta')) down.clear();
			else down.delete(e.code);
			if (armed === null || e.code !== armed) return false;
			armed = null;
			return true;
		},
		blur() {
			down.clear();
			armed = null;
		}
	};
}
