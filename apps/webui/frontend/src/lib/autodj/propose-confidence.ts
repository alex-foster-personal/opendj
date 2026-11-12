/** Planner confidence readout shared by the try-er and AutoPlay (TRANSAI, issue #10150). */

export function proposalConfidenceLabel(confidence: number, weakest: string): string {
	return `confidence ${confidence.toFixed(2)} (weakest: ${weakest})`;
}

export function proposalConfidenceTitle(factors: Record<string, number>): string {
	return ['grid', 'structure', 'vocal', 'gap']
		.map((name) => `${name} ${(factors[name] ?? 0).toFixed(2)}`)
		.join('; ');
}

export function proposalConfidenceReadout(
	confidence: number | null | undefined,
	weakest: string | null | undefined,
	factors: Record<string, number> | null | undefined
): { label: string | null; title: string | null } {
	if (
		confidence === null ||
		confidence === undefined ||
		weakest === null ||
		weakest === undefined
	) {
		return { label: null, title: null };
	}
	return {
		label: proposalConfidenceLabel(confidence, weakest),
		title: factors ? proposalConfidenceTitle(factors) : null
	};
}
