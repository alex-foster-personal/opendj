import { Fragment, useEffect, useRef, useState } from 'react';

import { plannedTitle } from '$lib/rb/planned-explainers';
import { clampToViewport } from '$lib/ui/clamp-to-viewport';

import './ContextMenu.react.css';

// React twin of ContextMenu.svelte: same DOM, roles, ARIA and keyboard
// behavior, held to the same contract (ContextMenu.contract.ts). An item with
// no `run` is unavailable: aria-disabled, explained by its title and a
// visually hidden description, and choosing it does nothing.

export interface ContextMenuItem {
	id: string;
	label: string;
	run?: (() => void | Promise<void>) | undefined;
	title?: string;
	testId?: string;
	checked?: boolean;
}

export type ContextMenuProps = {
	items: ContextMenuItem[];
	x: number;
	y: number;
	onclose: () => void;
};

export default function ContextMenu({ items, x, y, onclose }: ContextMenuProps) {
	const menuRef = useRef<HTMLDivElement>(null);
	const [position, setPosition] = useState({ x: 0, y: 0 });

	// Place after the DOM is committed (the Svelte original awaits tick()),
	// clamped to the viewport from the menu's measured size, then take focus.
	useEffect(() => {
		const menu = menuRef.current;
		if (menu === null) return;
		const rect = menu.getBoundingClientRect();
		setPosition(
			clampToViewport(
				x,
				y,
				{ width: rect.width, height: rect.height },
				{ width: window.innerWidth, height: window.innerHeight }
			)
		);
		menu.focus();
	}, [x, y]);

	// The latest props for the window listeners, which are bound once.
	const latest = useRef({ onclose });
	latest.current = { onclose };

	useEffect(() => {
		function menuItems(): HTMLElement[] {
			const menu = menuRef.current;
			if (menu === null) return [];
			return Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemcheckbox"]'));
		}

		function focusMenuItem(index: number): void {
			const all = menuItems();
			if (all.length === 0) return;
			const wrapped = ((index % all.length) + all.length) % all.length;
			all[wrapped].focus();
		}

		function onKeydown(event: KeyboardEvent): void {
			if (event.key === 'Escape') {
				event.preventDefault();
				latest.current.onclose();
				return;
			}

			const target = event.target;
			if (
				target instanceof HTMLElement &&
				target.getAttribute('aria-disabled') === 'true' &&
				(event.key === 'Enter' || event.key === ' ')
			) {
				event.preventDefault();
				return;
			}

			const menu = menuRef.current;
			if (menu === null || !menu.contains(document.activeElement)) return;

			const all = menuItems();
			if (all.length === 0) return;

			const active = document.activeElement;
			const currentIndex = active instanceof HTMLElement ? all.indexOf(active) : -1;

			if (event.key === 'ArrowDown') {
				event.preventDefault();
				focusMenuItem(currentIndex < 0 ? 0 : currentIndex + 1);
				return;
			}
			if (event.key === 'ArrowUp') {
				event.preventDefault();
				focusMenuItem(currentIndex < 0 ? all.length - 1 : currentIndex - 1);
				return;
			}
			if (event.key === 'Home') {
				event.preventDefault();
				focusMenuItem(0);
				return;
			}
			if (event.key === 'End') {
				event.preventDefault();
				focusMenuItem(all.length - 1);
			}
		}

		function onOutsidePointerDown(event: PointerEvent): void {
			const menu = menuRef.current;
			if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) latest.current.onclose();
		}

		window.addEventListener('keydown', onKeydown);
		window.addEventListener('pointerdown', onOutsidePointerDown, true);
		return () => {
			window.removeEventListener('keydown', onKeydown);
			window.removeEventListener('pointerdown', onOutsidePointerDown, true);
		};
	}, []);

	async function activate(item: ContextMenuItem): Promise<void> {
		if (item.run === undefined) return;
		onclose();
		await item.run();
	}

	return (
		<div
			ref={menuRef}
			className="context-menu"
			data-testid="context-menu"
			role="menu"
			tabIndex={-1}
			style={{ left: `${position.x}px`, top: `${position.y}px` }}
		>
			{items.map((item) => {
				const unavailable = item.run === undefined;
				const explanation = item.title ?? (unavailable ? plannedTitle('context-menu-unavailable') : item.label);
				const descId = `ctx-menu-desc-${item.id}`;
				const checkable = typeof item.checked === 'boolean';
				return (
					<Fragment key={item.id}>
						<button
							type="button"
							role={checkable ? 'menuitemcheckbox' : 'menuitem'}
							data-testid={item.testId}
							aria-disabled={unavailable ? true : undefined}
							aria-checked={checkable ? item.checked : undefined}
							aria-describedby={unavailable ? descId : undefined}
							title={explanation}
							onClick={() => void activate(item)}
						>
							{item.label}
						</button>
						{unavailable ? (
							<span id={descId} className="visually-hidden">
								{explanation}
							</span>
						) : null}
					</Fragment>
				);
			})}
		</div>
	);
}
