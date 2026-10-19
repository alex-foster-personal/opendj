import { useEffect, type ReactNode } from 'react';

import './EditSuiteModal.react.css';

// React twin of EditSuiteModal.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (EditSuiteModal.contract.ts).
// Shared modal shell for the edit-suite batch (find-replace / bulk-edit /
// mytag-editor). `children` stands in for the Svelte `children` snippet.

export type EditSuiteModalProps = {
	title: string;
	onclose: () => void;
	children: ReactNode;
};

export default function EditSuiteModal({ title, onclose, children }: EditSuiteModalProps) {
	// <svelte:window onkeydown>: Escape anywhere closes.
	useEffect(() => {
		const onKeydown = (e: KeyboardEvent): void => {
			if (e.key === 'Escape') onclose();
		};
		window.addEventListener('keydown', onKeydown);
		return () => window.removeEventListener('keydown', onKeydown);
	}, [onclose]);

	return (
		<div className="es-overlay" role="presentation" onClick={() => onclose()}>
			<div
				className="es-panel"
				role="dialog"
				aria-modal="true"
				aria-label={title}
				tabIndex={-1}
				onClick={(e) => e.stopPropagation()}
			>
				<div className="es-header">
					<h2>{title}</h2>
					<button className="es-close" onClick={() => onclose()} aria-label="close">
						x
					</button>
				</div>
				<div className="es-body">{children}</div>
			</div>
		</div>
	);
}
