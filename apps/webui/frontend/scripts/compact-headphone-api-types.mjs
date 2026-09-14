#!/usr/bin/env node
/**
 * Collapse generated headphone POST operations to one shared alias.
 * openapi-typescript pastes a 35-line request/response block per route;
 * jscpd scores that as duplication.percent and the quality ratchet fails.
 *
 * Idempotent. Leaves optional-body refresh as-is (already unique).
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const TARGET = fileURLToPath(new URL('../src/lib/api-types.ts', import.meta.url));
const ALIAS = '_HeadphoneJsonPost';
const MARKER = `export interface operations {`;
const ALIAS_DEF = `type ${ALIAS} = {
    parameters: {
        query?: never;
        header?: never;
        path?: never;
        cookie?: never;
    };
    requestBody: {
        content: {
            "application/json": {
                [key: string]: unknown;
            };
        };
    };
    responses: {
        200: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["HeadphoneStateOut"];
            };
        };
        422: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["HTTPValidationError"];
            };
        };
    };
};

`;

const OP_START =
	/^    (post_[A-Za-z0-9_]+_api_v1_performance_headphones_[A-Za-z0-9_]+_post): \{/gm;

function blockAt(text, openIdx) {
	let depth = 0;
	for (let i = openIdx; i < text.length; i += 1) {
		const ch = text[i];
		if (ch === '{') depth += 1;
		else if (ch === '}') {
			depth -= 1;
			if (depth === 0) return text.slice(openIdx, i + 1);
		}
	}
	throw new Error(`unbalanced headphone operation at ${openIdx}`);
}

function isRequiredJsonPost(block) {
	if (!block.includes('components["schemas"]["HeadphoneStateOut"]')) return false;
	if (!block.includes('components["schemas"]["HTTPValidationError"]')) return false;
	if (block.includes('requestBody?:')) return false;
	if (!block.includes('requestBody:')) return false;
	return true;
}

const source = readFileSync(TARGET, 'utf8');
let text = source;
if (!text.includes(`type ${ALIAS} = {`)) {
	const at = text.indexOf(MARKER);
	if (at < 0) throw new Error('operations interface missing from api-types.ts');
	text = `${text.slice(0, at)}${ALIAS_DEF}${text.slice(at)}`;
}

const replacements = [];
OP_START.lastIndex = 0;
let match = OP_START.exec(text);
while (match !== null) {
	const name = match[1];
	const openIdx = match.index + match[0].lastIndexOf('{');
	const block = blockAt(text, openIdx);
	if (isRequiredJsonPost(block)) {
		let end = openIdx + block.length;
		if (text[end] === ';') end += 1;
		replacements.push({
			start: match.index,
			end,
			name
		});
	}
	OP_START.lastIndex = openIdx + block.length;
	match = OP_START.exec(text);
}

if (replacements.length === 0 && source.includes(`: ${ALIAS};`)) {
	process.stdout.write('[OK] headphone api-types already compacted\n');
	process.exit(0);
}

let out = text;
for (let i = replacements.length - 1; i >= 0; i -= 1) {
	const item = replacements[i];
	out = `${out.slice(0, item.start)}    ${item.name}: ${ALIAS};${out.slice(item.end)}`;
}

if (out === source) {
	throw new Error('compact-headphone-api-types made no changes; generation shape drifted');
}
writeFileSync(TARGET, out);
process.stdout.write(`[OK] compacted ${String(replacements.length)} headphone POST operations\n`);
