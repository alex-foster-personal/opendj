export interface MyTagSweepConfirmation {
	action: 'delete' | 'rename';
	tagName: string;
	affectedTrackCount: number;
	destinationName?: string;
	requiresMergeConsent?: boolean;
}

export function formatMyTagSweepConfirmation(details: MyTagSweepConfirmation): string {
	const scope = `"${details.tagName}" from ${details.affectedTrackCount} track(s)`;
	if (details.action === 'delete') {
		return `Delete ${scope}? This cannot be undone.`;
	}
	if (details.destinationName === undefined) {
		throw new Error('rename confirmation requires destinationName');
	}
	const mergeNotice = details.requiresMergeConsent ? ' This merges it into an existing tag.' : '';
	return `Rename ${scope} to "${details.destinationName}"?${mergeNotice}`;
}

export async function runConfirmedMyTagSweep<T>(
	details: MyTagSweepConfirmation,
	confirm: (message: string) => boolean,
	mutate: () => Promise<T>
): Promise<T | null> {
	if (!confirm(formatMyTagSweepConfirmation(details))) return null;
	return mutate();
}
