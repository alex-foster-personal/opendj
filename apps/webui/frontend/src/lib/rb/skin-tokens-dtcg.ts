/**
 * W3C DTCG design-token export/import for a skin's colour scheme (issue
 * #13871, PLAN.md section 0: DTCG is the skin contract's token file).
 *
 * `toDtcg` turns a flat scheme map (`DARK_SCHEME_TOKENS` / `LIGHT_SCHEME_TOKENS`
 * in theme-tokens.ts) into a DTCG document: one `{ $type: 'color', $value }`
 * token per scheme key. `fromDtcg` reads one back. Groups are allowed on
 * import; a nested token's scheme key is its path joined with '-'. Only
 * `color` and `dimension` types are accepted; dimension tokens are returned
 * separately because the colour scheme has no slot for them. The imported
 * scheme is run through `validateScheme` and its violations are returned so
 * a caller cannot mistake a low-contrast skin for a clean one.
 */
import { validateScheme, type ContrastViolation } from './color-contrast';

export interface DtcgToken {
	$type: string;
	$value: string;
	$description?: string;
}

export type DtcgDocument = { [key: string]: DtcgToken | DtcgDocument };

export interface DtcgImport {
	tokens: Record<string, string>;
	dimensions: Record<string, string>;
	violations: ContrastViolation[];
}

const HEX_COLOUR = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;
const ALLOWED_TYPES = new Set(['color', 'dimension']);

export function toDtcg(tokens: Readonly<Record<string, string>>): DtcgDocument {
	const doc: DtcgDocument = {};
	for (const [name, value] of Object.entries(tokens)) {
		doc[name] = { $type: 'color', $value: value };
	}
	return doc;
}

function isToken(node: unknown): node is DtcgToken {
	return typeof node === 'object' && node !== null && '$value' in node;
}

export function fromDtcg(doc: DtcgDocument): DtcgImport {
	const tokens: Record<string, string> = {};
	const dimensions: Record<string, string> = {};
	const walk = (node: DtcgDocument, path: string[], inheritedType?: string): void => {
		const groupType =
			typeof node.$type === 'string' ? (node.$type as unknown as string) : inheritedType;
		for (const [key, child] of Object.entries(node)) {
			if (key.startsWith('$')) continue;
			const childPath = [...path, key];
			const dotted = childPath.join('.');
			if (isToken(child)) {
				const type = child.$type ?? groupType;
				if (typeof type !== 'string' || !ALLOWED_TYPES.has(type)) {
					throw new Error(`DTCG token ${dotted} has unsupported $type: ${String(type)}`);
				}
				const value = child.$value;
				const name = childPath.join('-');
				if (type === 'color') {
					if (typeof value !== 'string' || !HEX_COLOUR.test(value)) {
						throw new Error(
							`DTCG token ${dotted} color $value is not a hex colour: ${String(value)}`
						);
					}
					tokens[name] = value;
				} else {
					dimensions[name] = String(value);
				}
			} else if (typeof child === 'object' && child !== null) {
				walk(child as DtcgDocument, childPath, groupType);
			} else {
				throw new Error(`DTCG node ${dotted} is neither a token nor a group`);
			}
		}
	};
	walk(doc, []);
	return { tokens, dimensions, violations: validateScheme(tokens) };
}
