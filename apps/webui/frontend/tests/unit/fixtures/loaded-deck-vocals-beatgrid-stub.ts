/** The cache re-exports this lazy dependency; vocal refresh must never call it. */
export function upgradeDeckBeatgrid(): never {
	throw new Error('Beatgrid upgrades are outside the vocal refresh test');
}
