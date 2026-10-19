import './TreeCurrentFold.react.css';

// React twin of TreeCurrentFold.svelte (sticky CURRENT fold, pin 2ac3a0),
// held to the same contract (TreeCurrentFold.contract.ts).
export type TreeCurrentFoldProps = {
	fold: 'above' | 'below' | null;
	onjump: () => void;
};

export default function TreeCurrentFold({ fold, onjump }: TreeCurrentFoldProps) {
	if (fold === null) return null;
	const cls = ['current-fold', fold === 'above' && 'above', fold === 'below' && 'below'].filter(Boolean).join(' ');
	return (
		<button
			type="button"
			className={cls}
			onClick={() => onjump()}
			title={
				fold === 'above' ? 'Current playlist is above - click to jump' : 'Current playlist is below - click to jump'
			}
		>
			{fold === 'above' ? '▲' : '▼'} CURRENT
		</button>
	);
}
