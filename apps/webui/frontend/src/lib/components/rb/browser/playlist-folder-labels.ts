export function hiddenBrokenLabel(count: number): string {
	return count === 1
		? '1 playlist hidden by Broken filter'
		: `${count} playlists hidden by Broken filter`;
}
