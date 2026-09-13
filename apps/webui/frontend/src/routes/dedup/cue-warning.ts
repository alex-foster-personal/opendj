import type { ClusterMember } from './types';

export function mergeCueWarning(
	members: ClusterMember[],
	survivorId: string
): string | null {
	const survivor = members.find((member) => member.stable_id === survivorId);
	if (survivor === undefined) {
		return null;
	}
	if (survivor.cue_count > 0) {
		return null;
	}
	const othersWithCues = members.filter(
		(member) => member.stable_id !== survivorId && member.cue_count > 0
	);
	if (othersWithCues.length === 0) {
		return null;
	}
	const totalCues = othersWithCues.reduce((sum, member) => sum + member.cue_count, 0);
	if (othersWithCues.length === 1) {
		return (
			`Selected survivor has no cue points. 1 other copy has ${totalCues} cues. ` +
			'Merge does not copy cues onto the survivor. Continue anyway?'
		);
	}
	return (
		`Selected survivor has no cue points. ${othersWithCues.length} other copies have ${totalCues} cues. ` +
		'Merge does not copy cues onto the survivor. Continue anyway?'
	);
}
