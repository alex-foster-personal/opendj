/**
 * Session-wide "SET OUTPUTS was clicked" flag (pin 894af5672c3b). Module
 * level so a remount of the mixer row does not restart the red pulse; seeded
 * from sessionStorage so a reload within the same tab session does not either.
 */
import { markIoClickedThisSession, readIoClickedThisSession } from './io-outputs-button';

export const ioOutputsSession = $state({ clicked: readIoClickedThisSession() });

export function noteSetOutputsClicked(): void {
	ioOutputsSession.clicked = true;
	markIoClickedThisSession();
}
