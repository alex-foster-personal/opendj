/**
 * The Cmd+I (Ctrl+I off Mac) assistant sidebar toggle rule. Pure and tiny on
 * purpose: it ships in the first-paint closure with the toggle button, while
 * the sidebar itself is a lazy chunk.
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import {
	ASSISTANT_CHORD_CODE,
	ASSISTANT_CHORD_KEY,
	ASSISTANT_PLATFORM,
	assistantChordUsesMeta
} from '$lib/components/rb/hotkeys/hotkeys-registry';
import type { BootScheduler } from '$lib/rb/boot-scheduler';

export interface ToggleKeyEvent {
	key: string;
	code: string;
	ctrlKey: boolean;
	metaKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
	target: EventTarget | null;
}

/** Marks the sidebar root, whose own composer still takes the chord. */
export const ASSISTANT_ROOT_ATTR = 'data-assistant-sidebar';

/** The platform's own modifier (Cmd on Apple, Ctrl elsewhere) and no other, never while typing in a field outside the sidebar. */
export function isAssistantToggleKey(
	e: ToggleKeyEvent,
	platform: string = ASSISTANT_PLATFORM
): boolean {
	const onlyPrimary = assistantChordUsesMeta(platform)
		? e.metaKey && !e.ctrlKey
		: e.ctrlKey && !e.metaKey;
	if (!onlyPrimary || e.altKey || e.shiftKey) return false;
	if (e.key.toLowerCase() !== ASSISTANT_CHORD_KEY && e.code !== ASSISTANT_CHORD_CODE) return false;
	return !isTextEntryTarget(e.target) || inAssistant(e.target);
}

export interface FocusTarget {
	isConnected: boolean;
	focus(): void;
}

/** Closing returns focus to what had it at open, if that still exists outside the sidebar. */
export function focusReturnTarget<T extends FocusTarget>(opener: T | null): T | null {
	return opener !== null && opener.isConnected && !inAssistant(opener) ? opener : null;
}

/**
 * AISIDE-7892f: closing hands focus back to the opener, otherwise nothing is
 * focused. An opener that cannot take focus (gone, or the page body) must not
 * leave focus on a control the close is about to hide.
 */
export function settleFocusOnClose<T extends FocusTarget>(
	opener: T | null,
	activeElement: () => { blur(): void } | null
): void {
	focusReturnTarget(opener)?.focus();
	const active = activeElement();
	if (active !== null && inAssistant(active)) active.blur();
}

function inAssistant(target: unknown): boolean {
	const closest = (target as Element | null)?.closest;
	return typeof closest === 'function' && closest.call(target, `[${ASSISTANT_ROOT_ATTR}]`) !== null;
}

/** AISIDE-7892j: with the flag off the chord is not ours, so the key event passes through. */
export function assistantChordApplies(
	enabled: boolean,
	e: ToggleKeyEvent,
	platform: string = ASSISTANT_PLATFORM
): boolean {
	return enabled && isAssistantToggleKey(e, platform);
}

/**
 * Queue the sidebar's lazy mount behind the boot window, off the first paint.
 * A failed load, or an `onReady` that throws, reaches `onError`: deferred, a
 * missing chunk would otherwise vanish without a word.
 */
export function deferLoad<T>(
	label: string,
	load: () => Promise<T>,
	onReady: (loaded: T) => void,
	onError: (error: unknown) => void,
	scheduler: Pick<BootScheduler, 'defer'>
): void {
	scheduler.defer(label, () => {
		void load().then(onReady).catch(onError);
	});
}
