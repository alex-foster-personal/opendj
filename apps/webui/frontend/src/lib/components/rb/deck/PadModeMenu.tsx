import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import type { DeckId } from '$lib/rb/deck-id';
import { PAD_MODE_CATALOG, padModeMenuLabel } from '$lib/rb/pad-mode-catalog';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { padModeFor, padModeSelectable } from '$lib/rb/pad-mode-state.svelte';
import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

import './PadModeMenu.react.css';

// React twin of PadModeMenu.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (PadModeMenu.contract.ts). Hot Cue, Beat Jump (DECKUX-9956) and
// Pad FX 1/2 are built; a mode that cannot be selected renders inert with its reason as the tooltip. The menu is
// position: fixed, placed from the trigger's rect by triggerFloatingAction
// (the same helper the Svelte `use:` action runs), because the deck clips overflow.

export type PadModeMenuProps = { deckId: DeckId; stacked?: boolean };

export default function PadModeMenu({ deckId, stacked = false }: PadModeMenuProps) {
	const [open, setOpen] = useState(false);
	const mode = padModeFor(deckId);
	const triggerLabel = (
		PAD_MODE_CATALOG.find((entry) => entry.id === mode)?.label ?? 'Hot Cue'
	).toUpperCase();
	const wrapRef = useRef<HTMLSpanElement>(null);
	const triggerRef = useRef<HTMLButtonElement>(null);
	const menuRef = useRef<HTMLDivElement>(null);

	useEffect(() => {
		function onWindowPointerDown(e: PointerEvent): void {
			if (!open) return;
			const target = e.target;
			if (target instanceof Node && wrapRef.current?.contains(target)) return;
			setOpen(false);
		}
		function onWindowKeyDown(e: KeyboardEvent): void {
			if (!open || e.key !== 'Escape') return;
			setOpen(false);
			triggerRef.current?.focus();
		}
		window.addEventListener('pointerdown', onWindowPointerDown);
		window.addEventListener('keydown', onWindowKeyDown);
		return () => {
			window.removeEventListener('pointerdown', onWindowPointerDown);
			window.removeEventListener('keydown', onWindowKeyDown);
		};
	}, [open]);

	useLayoutEffect(() => {
		const node = menuRef.current;
		if (!open || node === null) return;
		const action = triggerFloatingAction(node, {
			getTrigger: () => triggerRef.current,
			preferred: 'below',
			gap: 4
		});
		return action.destroy;
	}, [open]);

	return (
		<span className="pad-menu-wrap" ref={wrapRef}>
			<button
				ref={triggerRef}
				className={stacked ? 'rb-lit-button dropdown stacked' : 'rb-lit-button dropdown'}
				aria-label={`hot cue menu deck ${deckId}`}
				title="Pad mode menu - lists the pad modes a controller's performance-pad selector offers; modes marked not-built-yet do nothing yet"
				aria-expanded={open}
				data-testid={`hot-cue-menu-deck-${deckId}`}
				onClick={() => setOpen(!open)}
			>
				{triggerLabel}
			</button>
			{open ? (
				<div className="pad-menu" role="menu" ref={menuRef}>
					{PAD_MODE_CATALOG.map((entry) => {
						const sel = padModeSelectable(deckId, entry);
						return (
							<button
								key={entry.id}
								type="button"
								className={sel.enabled ? 'pad-menu-item' : 'pad-menu-item rb-inert'}
								role="menuitem"
								data-pad-mode={entry.id}
								data-built={entry.built}
								disabled={!sel.enabled}
								title={sel.enabled ? undefined : sel.reason}
								onClick={() => {
									setOpen(false);
									void runPerformanceCommandFromUi({
										type: 'pad_mode',
										deck: deckId,
										mode: entry.id
									});
								}}
							>
								{padModeMenuLabel(entry)}
							</button>
						);
					})}
				</div>
			) : null}
		</span>
	);
}
