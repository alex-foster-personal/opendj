import { useState } from 'react';

import './BrowserConfirmDialog.react.css';

// React twin of BrowserConfirmDialog.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (BrowserConfirmDialog.contract.ts). The Svelte
// `open` is $bindable and the dialog closes itself after either button; here
// it is controlled, the React form of bind:open: the dialog renders straight
// from `open` and asks the parent to close it through onOpenChange(false), so
// there is one copy of the flag and a later open={true} always reopens it.

type ConfirmOpts = { remember: boolean; setDefault: boolean };

export type BrowserConfirmDialogProps = {
	open: boolean;
	title: string;
	message: string;
	primaryLabel?: string;
	secondaryLabel?: string;
	showRemember?: boolean;
	rememberLabel?: string;
	showDefault?: boolean;
	defaultLabel?: string;
	onPrimary: (opts: ConfirmOpts) => void;
	onSecondary?: (opts: ConfirmOpts) => void;
	onOpenChange: (open: boolean) => void;
};

export default function BrowserConfirmDialog({
	open,
	title,
	message,
	primaryLabel = 'OK',
	secondaryLabel = 'Cancel',
	showRemember = true,
	rememberLabel = "Don't show this again",
	showDefault = false,
	defaultLabel = 'Default to this',
	onPrimary,
	onSecondary,
	onOpenChange
}: BrowserConfirmDialogProps) {
	const [remember, setRemember] = useState(false);
	const [setDefault, setSetDefault] = useState(false);

	function close(): void {
		setRemember(false);
		setSetDefault(false);
		onOpenChange(false);
	}

	if (!open) return null;
	return (
		<div className="browser-confirm" role="dialog" aria-modal="true" aria-label={title}>
			<p className="browser-confirm-title">{title}</p>
			<p className="browser-confirm-msg">{message}</p>
			{showDefault && (
				<label className="browser-confirm-opt">
					<input type="checkbox" checked={setDefault} onChange={(e) => setSetDefault(e.currentTarget.checked)} />
					{defaultLabel}
				</label>
			)}
			{showRemember && (
				<label className="browser-confirm-opt">
					<input type="checkbox" checked={remember} onChange={(e) => setRemember(e.currentTarget.checked)} />
					{rememberLabel}
				</label>
			)}
			<div className="browser-confirm-actions">
				<button
					type="button"
					className="browser-confirm-primary"
					onClick={() => {
						onPrimary({ remember, setDefault });
						close();
					}}
				>
					{primaryLabel}
				</button>
				<button
					type="button"
					className="browser-confirm-secondary"
					onClick={() => {
						onSecondary?.({ remember, setDefault });
						close();
					}}
				>
					{secondaryLabel}
				</button>
			</div>
		</div>
	);
}
