/**
 * Compile a skin's flat `--rb-*` token map into one scoped CSS property block.
 * Values only: anything that could escape the declaration block is refused.
 */

const SKIN_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const TOKEN_NAME = /^--rb-[a-z0-9-]+$/;
const FORBIDDEN = [';', '{', '}', 'url(', '@import', '</'];

export function compileSkinTokens(id: string, tokens: Record<string, string>): string {
	if (typeof id !== 'string' || !SKIN_ID.test(id)) {
		throw new Error(`invalid skin id: ${String(id)}`);
	}
	const names = Object.keys(tokens).sort();
	const lines = names.map((name) => {
		if (!TOKEN_NAME.test(name)) throw new Error(`invalid token name: ${name}`);
		const value = String(tokens[name]).trim();
		const lower = value.toLowerCase();
		if (!value || FORBIDDEN.some((bad) => lower.includes(bad))) {
			throw new Error(`invalid value for token ${name}`);
		}
		return `\t${name}: ${value};`;
	});
	return `html[data-skin="${id}"] .perf-root {\n${lines.join('\n')}\n}\n`;
}
