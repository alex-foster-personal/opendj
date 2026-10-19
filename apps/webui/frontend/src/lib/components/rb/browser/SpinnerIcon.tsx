import './SpinnerIcon.react.css';

// React twin of SpinnerIcon.svelte: same DOM, held to SpinnerIcon.contract.ts.
export type SpinnerIconProps = { size?: number };

export default function SpinnerIcon({ size = 10 }: SpinnerIconProps) {
	return (
		<svg className="spinner-icon" viewBox="0 0 16 16" width={size} height={size} aria-hidden="true">
			<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
			<path d="M8 8V3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
		</svg>
	);
}
