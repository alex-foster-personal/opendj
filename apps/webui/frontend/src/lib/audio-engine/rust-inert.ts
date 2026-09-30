/**
 * Rust engine mode grays out the controls it cannot play (NAE-15).
 *
 * The performance page's controls are spread over many components, and most
 * already name themselves with `data-performance-control`. Rather than thread a
 * Rust-mode flag through each of them, this maps those names to the command the
 * control sends and marks the unsupported ones inert in place: disabled, the
 * house PARITY-TODO title, `aria-disabled`, and a capture-phase guard so a
 * knob or a re-enabled button cannot send anyway. A control without a
 * `data-performance-control` name opts in with `data-rust-command`.
 *
 * Which commands are unsupported is `rustCommandUnsupported`: the engine's own
 * hello list, the Web Audio only features, and key lock until the engine
 * reports it. When a better engine connects, the controls light up again.
 */
import { rustCommandUnsupported } from './rust-mode.svelte';

export const RUST_INERT_TITLE = 'not implemented - see PARITY-TODO';

/** `data-performance-control` name -> the command that control sends. */
export const CONTROL_COMMANDS: Readonly<Record<string, string>> = {
	slip: 'slip',
	'master-tempo': 'master_tempo',
	'key-sync': 'key_sync',
	'key-nudge-up': 'key_nudge',
	'key-nudge-down': 'key_nudge',
	'safety-loop-save': 'safety_loop_save',
	'safety-loop-arm': 'safety_loop_arm',
	'safety-loop-clear': 'safety_loop_clear',
	'loop-interval-mode': 'loop_interval_mode',
	'loop-interval-up': 'loop_interval_base',
	'loop-interval-down': 'loop_interval_base',
	'loop-interval': 'loop_interval_base',
	headphones: 'headphone_mix',
	'cue-calibrate': 'headphone_calibrate',
	'head-delay': 'head_delay_ms'
};

/** The command a control element sends, or null when it is not mapped. */
export function commandForControl(control: string | undefined, explicit?: string): string | null {
	if (explicit !== undefined && explicit.length > 0) return explicit;
	if (control === undefined) return null;
	if (control.startsWith('stem-')) return 'stem_mute';
	return CONTROL_COMMANDS[control] ?? null;
}

type Saved = { title: string | null; disabled: boolean | undefined };
const saved = new WeakMap<HTMLElement, Saved>();

function _formControls(el: HTMLElement): HTMLElement[] {
	return el.matches('button, input, select')
		? [el]
		: [...el.querySelectorAll<HTMLElement>('button, input, select')];
}

function _hold(el: HTMLElement): void {
	const f = el as HTMLElement & { disabled?: boolean };
	if (!saved.has(el)) {
		saved.set(el, { title: el.getAttribute('title'), disabled: f.disabled });
	}
	if (f.disabled === false) f.disabled = true;
	if (el.getAttribute('title') !== RUST_INERT_TITLE) el.setAttribute('title', RUST_INERT_TITLE);
	if (el.getAttribute('aria-disabled') !== 'true') el.setAttribute('aria-disabled', 'true');
}

function _release(el: HTMLElement): void {
	const s = saved.get(el);
	if (s === undefined) return;
	saved.delete(el);
	const f = el as HTMLElement & { disabled?: boolean };
	if (s.disabled !== undefined) f.disabled = s.disabled;
	if (s.title === null) el.removeAttribute('title');
	else el.setAttribute('title', s.title);
	el.removeAttribute('aria-disabled');
}

/**
 * Mark every unsupported control under `root` inert, and release the ones that
 * are supported again. Returns the commands now held inert, for tests and the
 * badge's hover text.
 */
export function applyRustInert(root: ParentNode): string[] {
	const held = new Set<string>();
	for (const el of root.querySelectorAll<HTMLElement>('[data-performance-control], [data-rust-command]')) {
		const cmd = commandForControl(el.dataset.performanceControl, el.dataset.rustCommand);
		if (cmd === null) continue;
		const off = rustCommandUnsupported(cmd);
		if (off) {
			held.add(cmd);
			el.dataset.rustInert = cmd;
			el.classList.add('rust-inert');
			if (!el.matches('button, input, select')) _hold(el);
			for (const c of _formControls(el)) _hold(c);
		} else if (el.dataset.rustInert !== undefined) {
			delete el.dataset.rustInert;
			el.classList.remove('rust-inert');
			if (!el.matches('button, input, select')) _release(el);
			for (const c of _formControls(el)) _release(c);
		}
	}
	return [...held].sort();
}

/**
 * A component re-rendering an inert control can set its `disabled` or `title`
 * back. Keep the hold, and remember the component's latest values so the
 * release restores what the component wants now, not what it had then.
 */
function _reassert(records: MutationRecord[]): void {
	for (const r of records) {
		const el = r.target as HTMLElement;
		const s = saved.get(el);
		if (s === undefined) continue;
		const f = el as HTMLElement & { disabled?: boolean };
		if (r.attributeName === 'title' && el.getAttribute('title') !== RUST_INERT_TITLE) {
			s.title = el.getAttribute('title');
		}
		if (r.attributeName === 'disabled' && f.disabled === false) s.disabled = false;
		_hold(el);
	}
}

const BLOCKED = ['pointerdown', 'mousedown', 'click', 'keydown', 'wheel', 'input', 'change'] as const;

function _block(e: Event): void {
	const t = e.target;
	if (!(t instanceof Element) || t.closest('[data-rust-inert]') === null) return;
	e.preventDefault();
	e.stopImmediatePropagation();
}

/**
 * Keep `root` in step: re-apply on DOM changes (controls mount as decks load)
 * and hold inert controls against re-renders. Call `refresh` when what is
 * supported changes; `stop` releases everything.
 */
export function watchRustInert(root: HTMLElement): { refresh: () => string[]; stop: () => void } {
	let queued = false;
	const refresh = (): string[] => applyRustInert(root);
	const structure = new MutationObserver(() => {
		if (queued) return;
		queued = true;
		queueMicrotask(() => {
			queued = false;
			refresh();
		});
	});
	structure.observe(root, { childList: true, subtree: true });
	const attrs = new MutationObserver(_reassert);
	attrs.observe(root, { attributes: true, subtree: true, attributeFilter: ['disabled', 'title'] });
	for (const type of BLOCKED) root.addEventListener(type, _block, true);
	refresh();
	return {
		refresh,
		stop: () => {
			structure.disconnect();
			attrs.disconnect();
			for (const type of BLOCKED) root.removeEventListener(type, _block, true);
			for (const el of root.querySelectorAll<HTMLElement>('[data-rust-inert]')) {
				delete el.dataset.rustInert;
				el.classList.remove('rust-inert');
				if (!el.matches('button, input, select')) _release(el);
				for (const c of _formControls(el)) _release(c);
			}
		}
	};
}
