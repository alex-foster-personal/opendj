import './SecondaryLoadBadge.react.css';

// React twin of SecondaryLoadBadge.svelte: same DOM, classes, data attributes
// and title, held to the same contract (SecondaryLoadBadge.contract.ts).
// Renders nothing once the load settles; no live-ticking counter, on purpose.

export type SecondaryLoadBadgeProps = { status: string; error?: string | null; phase?: string | null };

export default function SecondaryLoadBadge({ status, error = null, phase = null }: SecondaryLoadBadgeProps) {
	// `unavailable` is a SETTLED answer - this track has no stem bundle - so it
	// gets no badge, the same as `ready`. Only in-flight and failed states show.
	const visible = status === 'loading' || status === 'error';
	if (!visible) return null;
	const failed = status === 'error';
	const hint = failed
		? `Stems unavailable - ${error ?? 'unknown error'}. The deck plays from its mix; stem controls stay off.`
		: `Loading stems in the background${phase === null ? '' : ` (${phase})`}. The deck is already playable - this only gates the VOCAL / INST / DRUMS controls.`;
	return (
		<span
			className={failed ? 'secondary-load failed' : 'secondary-load'}
			data-testid="deck-secondary-load"
			data-stem-load={status}
			data-stem-phase={phase ?? undefined}
			title={hint}
		>
			{failed ? 'STEMS!' : 'STEMS'}
		</span>
	);
}
