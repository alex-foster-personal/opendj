import { ACTION_LABELS, type PreflightActionId, type PreflightExplainer } from '$lib/preflight/preflight-actions';

import './PreflightActions.react.css';

// React twin of PreflightActions.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (PreflightActions.contract.ts).
// PREFLIGHT-05: action buttons, their click status line, the footer note and
// the static explainer lines, in that order. No button carries a `title`
// (see the original for why).

export type PreflightActionsProps = {
	actions: PreflightActionId[];
	disabled?: PreflightActionId[];
	pending?: PreflightActionId | null;
	status?: string | null;
	footer?: string | null;
	explainers: PreflightExplainer[];
	onAction?: (id: PreflightActionId) => unknown;
};

export default function PreflightActions({
	actions,
	disabled = [],
	pending = null,
	status = null,
	footer = null,
	explainers,
	onAction = () => {}
}: PreflightActionsProps) {
	return (
		<>
			<div className="preflight-actions">
				{actions.map((id) => {
					const cls = ['preflight-action', id === 'import' && 'preflight-import', pending === id && 'pending']
						.filter(Boolean)
						.join(' ');
					return (
						<button
							key={id}
							type="button"
							className={cls}
							data-testid={`preflight-action-${id}`}
							aria-busy={pending === id}
							disabled={pending !== null || disabled.includes(id)}
							onClick={() => onAction(id)}
						>
							{id === 'import' && pending === id ? 'Opening setup...' : ACTION_LABELS[id]}
						</button>
					);
				})}
			</div>
			<p className="preflight-action-status" data-testid="preflight-action-status" role="status" aria-live="polite">
				{status ?? ''}
			</p>
			{footer ? <p className="note">{footer}</p> : null}
			<ul className="note preflight-explainers" data-testid="preflight-explainers">
				{explainers.map((line) => (
					<li key={line.label}>
						<strong>{line.label}</strong>
						{`: ${line.text}`}
					</li>
				))}
			</ul>
		</>
	);
}
