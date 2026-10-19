import './LoadBlendHud.react.css';

// React twin of LoadBlendHud.svelte: same DOM, held to LoadBlendHud.contract.ts.
/** Tiny readout for the Load-to-CH click-drag intro blend. No engine writes. */
export type LoadBlendHudProps = { t: number; fader: number; scrubDeltaMs: number };

export default function LoadBlendHud({ t, fader, scrubDeltaMs }: LoadBlendHudProps) {
	const scrubSign = scrubDeltaMs >= 0 ? '+' : '';
	return (
		<div className="load-blend-hud" data-testid="load-blend-hud">
			<span>{`t ${t.toFixed(2)}`}</span>
			<span>{`scrub ${scrubSign}${Math.round(scrubDeltaMs)} ms / fader ${fader.toFixed(2)}`}</span>
		</div>
	);
}
