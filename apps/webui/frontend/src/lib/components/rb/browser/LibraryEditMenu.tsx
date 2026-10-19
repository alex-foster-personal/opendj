import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

import './LibraryEditMenu.react.css';

// React twin of LibraryEditMenu.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (LibraryEditMenu.contract.ts). The library's tag
// edits live behind ONE pencil (LIBUX-49). The list is always mounted and only
// shown while open, so the LESS view can hide items via data-less-hidden; it is
// position: fixed, placed from the pencil's rect by triggerFloatingAction.

type EditAction = 'find-replace' | 'bulk-edit' | 'mytag';

export type LibraryEditMenuProps = { hasSelection: boolean; onpick: (action: EditAction) => void };

const NEEDS_SELECTION = 'Select one or more tracks first';

export default function LibraryEditMenu({ hasSelection, onpick }: LibraryEditMenuProps) {
	const [open, setOpen] = useState(false);
	const wrapRef = useRef<HTMLSpanElement>(null);
	const triggerRef = useRef<HTMLButtonElement>(null);
	const listRef = useRef<HTMLDivElement>(null);

	// While the menu is open it is the one explainer: data-custom-tip tells the
	// shared tooltip layer to draw no box, and the dispatched pointerover makes
	// that layer re-read it in engines that do not re-fire one on DOM change.
	useEffect(() => {
		if (open) triggerRef.current?.dispatchEvent(new PointerEvent('pointerover', { bubbles: true }));
	}, [open]);

	useEffect(() => {
		function onWindowPointerDown(e: PointerEvent): void {
			if (!open) return;
			if (e.target instanceof Node && wrapRef.current?.contains(e.target)) return;
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
		const node = listRef.current;
		if (node === null) return;
		const action = triggerFloatingAction(node, {
			getTrigger: () => triggerRef.current,
			preferred: 'below',
			gap: 4
		});
		return action.destroy;
	}, []);

	function pick(action: EditAction): void {
		setOpen(false);
		onpick(action);
	}

	return (
		<span className="library-edit-menu" ref={wrapRef} data-custom-tip={open ? '' : undefined}>
			<button
				ref={triggerRef}
				type="button"
				className="rb-lit-button edit-trigger"
				aria-label="Edit tags"
				aria-haspopup="menu"
				aria-expanded={open}
				title="Edit tags - Find & Replace and Bulk Edit for the selected tracks, and MyTags"
				data-testid="library-edit-menu"
				onClick={() => setOpen(!open)}
			>
				<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
					<path
						d="M11.3 1.6a1.6 1.6 0 0 1 2.3 0l.8.8a1.6 1.6 0 0 1 0 2.3L5.6 13.5l-3.6.9.9-3.6z"
						fill="none"
						stroke="currentColor"
						strokeWidth="1.4"
						strokeLinejoin="round"
					/>
					<path d="M9.8 3.1l3.1 3.1" stroke="currentColor" strokeWidth="1.4" />
				</svg>
			</button>
			<div
				ref={listRef}
				className={open ? 'edit-menu-list open' : 'edit-menu-list'}
				role="menu"
				aria-label="Edit tags"
				data-testid="library-edit-menu-list"
				data-hover-card=""
			>
				<button
					type="button"
					className="rb-lit-button"
					role="menuitem"
					data-less-hidden=""
					disabled={!hasSelection}
					title={hasSelection ? undefined : NEEDS_SELECTION}
					onClick={() => pick('find-replace')}
				>
					Find &amp; Replace
				</button>
				<button
					type="button"
					className="rb-lit-button"
					role="menuitem"
					data-less-hidden=""
					disabled={!hasSelection}
					title={hasSelection ? undefined : NEEDS_SELECTION}
					onClick={() => pick('bulk-edit')}
				>
					Bulk Edit
				</button>
				<button type="button" className="rb-lit-button" role="menuitem" onClick={() => pick('mytag')}>
					MyTags
				</button>
			</div>
		</span>
	);
}
