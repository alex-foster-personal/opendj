import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { resolveCommentPinHotkey } from './comment-pin-hotkey-routing';
import { isNativeInteractiveTarget } from './performance-hotkeys-target';

export type PerformanceShortcutAction =
	| { kind: 'space'; quantize: boolean }
	| { kind: 'm' }
	| { kind: 'loop-resize'; factor: 0.5 | 2 }
	| { kind: 'loop-exit' };

export interface PerformanceShortcutKeyEvent {
	code: string;
	key: string;
	target: EventTarget | null;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey?: boolean;
}

export interface PerformanceShortcutKeydownEvent extends PerformanceShortcutKeyEvent {
	preventDefault: () => void;
	timeStamp: number;
}

export interface PerformanceShortcutHandlers {
	toggleRecentPlay: (pressT0Ms: number, quantize: boolean) => void;
	resizeLast: (factor: 0.5 | 2) => void;
	exitLast: () => void;
	armPinPlacement: () => void;
}

/** Pure routing for /performance global shortcuts; null means leave the event alone. */
export function resolvePerformanceShortcutAction(
	e: PerformanceShortcutKeyEvent
): PerformanceShortcutAction | null {
	if (e.code === 'Space' || e.key === ' ') {
		// Focused controls own Space activation; page chrome keeps transport.
		if (isTextEntryTarget(e.target)) return null;
		if (isNativeInteractiveTarget(e.target)) return null;
		if (e.metaKey || e.ctrlKey) {
			if (e.altKey) return null;
			return { kind: 'space', quantize: true };
		}
		if (e.altKey) return null;
		return { kind: 'space', quantize: false };
	}
	if (e.key === 'm' || e.key === 'M') {
		return resolveCommentPinHotkey({
			key: e.key,
			target: e.target,
			metaKey: e.metaKey,
			ctrlKey: e.ctrlKey,
			altKey: e.altKey,
			shiftKey: e.shiftKey ?? false
		}) === 'arm'
			? { kind: 'm' }
			: null;
	}
	if (isTextEntryTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return null;
	// Tab always traverses focus, including from body and inert page chrome.
	if (e.key === 'Tab') return null;
	if (e.key === '+' || e.key === '=') return { kind: 'loop-resize', factor: 2 };
	if (e.key === '-' || e.key === '_') return { kind: 'loop-resize', factor: 0.5 };
	if (e.key === ')') return { kind: 'loop-exit' };
	return null;
}

/** Applies a resolved shortcut: preventDefault plus the registered side effect. */
export function handlePerformanceShortcutKeydown(
	e: PerformanceShortcutKeydownEvent,
	handlers: PerformanceShortcutHandlers,
	options: { settingsOpen?: boolean } = {}
): boolean {
	if (options.settingsOpen === true) return false;
	const action = resolvePerformanceShortcutAction(e);
	if (action === null) return false;
	e.preventDefault();
	switch (action.kind) {
		case 'space':
			handlers.toggleRecentPlay(e.timeStamp, action.quantize);
			break;
		case 'loop-resize':
			handlers.resizeLast(action.factor);
			break;
		case 'loop-exit':
			handlers.exitLast();
			break;
		case 'm':
			handlers.armPinPlacement();
			break;
	}
	return true;
}
