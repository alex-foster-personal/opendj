/**
 * Shared gate for window capture-phase page shortcuts (issue #14604).
 *
 * A capture listener on window runs before any dialog can stop the event, so
 * a modal's own stopPropagation never shields it. Shortcut installers ask
 * this instead: a key whose target sits inside an open `aria-modal="true"`
 * dialog belongs to that dialog, never to the page behind it.
 */

/** True when the key event target sits inside an open aria-modal dialog. */
export function isModalDialogTarget(target: EventTarget | null): boolean {
	return target instanceof Element && target.closest('[aria-modal="true"]') !== null;
}
