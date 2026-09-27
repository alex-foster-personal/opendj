/**
 * Parse @keyframes blocks from ControlExplainer.svelte style text.
 */

const ALLOWED = new Set(['transform', 'opacity']);

/**
 * @param {string} svelteSource full .svelte file contents
 * @returns {string} unscoped style block inner text
 */
export function extractStyleBlock(svelteSource) {
	const start = svelteSource.indexOf('<style>');
	const end = svelteSource.indexOf('</style>');
	if (start === -1 || end === -1 || end <= start) {
		throw new Error('ControlExplainer.svelte: missing <style> block');
	}
	return svelteSource.slice(start + '<style>'.length, end);
}

/**
 * @param {string} styleText
 * @returns {Map<string, { offset: number, properties: string[] }[]>}
 */
export function parseKeyframesFromStyle(styleText) {
	const map = new Map();
	const re = /@keyframes\s+([a-zA-Z0-9_-]+)\s*\{/g;
	let match;
	while ((match = re.exec(styleText)) !== null) {
		const name = match[1];
		const bodyStart = match.index + match[0].length;
		let depth = 1;
		let i = bodyStart;
		while (i < styleText.length && depth > 0) {
			const ch = styleText[i];
			if (ch === '{') depth += 1;
			else if (ch === '}') depth -= 1;
			i += 1;
		}
		const body = styleText.slice(bodyStart, i - 1);
		map.set(name, parseKeyframeSteps(body));
	}
	return map;
}

/**
 * @param {string} body inner text of one @keyframes rule
 * @returns {{ offset: number, properties: string[] }[]}
 */
export function parseKeyframeSteps(body) {
	const steps = [];
	const stepRe =
		/((?:[0-9.]+%|\bfrom\b|\bto\b)(?:\s*,\s*(?:[0-9.]+%|\bfrom\b|\bto\b))*)\s*\{/gi;
	let stepMatch;
	while ((stepMatch = stepRe.exec(body)) !== null) {
		const selector = stepMatch[1].toLowerCase();
		const blockStart = stepMatch.index + stepMatch[0].length;
		let depth = 1;
		let j = blockStart;
		while (j < body.length && depth > 0) {
			const ch = body[j];
			if (ch === '{') depth += 1;
			else if (ch === '}') depth -= 1;
			j += 1;
		}
		const declarations = body.slice(blockStart, j - 1);
		const offsets = parseStepOffsets(selector);
		const properties = parseDeclarationProperties(declarations);
		for (const offset of offsets) {
			steps.push({ offset, properties: [...properties].sort() });
		}
	}
	steps.sort((a, b) => a.offset - b.offset || a.properties.join(',').localeCompare(b.properties.join(',')));
	return dedupeSteps(steps);
}

/**
 * @param {string} selector e.g. "0%", "45%", "0%, 100%", "from", "to"
 * @returns {number[]}
 */
function parseStepOffsets(selector) {
	const parts = selector.split(',').map((p) => p.trim());
	const offsets = [];
	for (const part of parts) {
		if (part === 'from') offsets.push(0);
		else if (part === 'to') offsets.push(1);
		else if (part.endsWith('%')) {
			offsets.push(Number.parseFloat(part.slice(0, -1)) / 100);
		} else {
			throw new Error(`unrecognized keyframe step selector: ${selector}`);
		}
	}
	return offsets;
}

/**
 * @param {string} declarations CSS declaration block
 * @returns {string[]} sorted unique property names
 */
function parseDeclarationProperties(declarations) {
	const props = new Set();
	const declRe = /([a-zA-Z-]+)\s*:/g;
	let m;
	while ((m = declRe.exec(declarations)) !== null) {
		const name = m[1].trim();
		if (name.startsWith('--')) continue;
		props.add(name);
	}
	return [...props].sort();
}

/**
 * @param {{ offset: number, properties: string[] }[]} steps
 */
function dedupeSteps(steps) {
	const seen = new Set();
	const out = [];
	for (const step of steps) {
		const key = `${step.offset}:${step.properties.join(',')}`;
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(step);
	}
	return out;
}

/**
 * @param {Record<string, readonly { offset: number, properties: readonly string[] }[]>} declared
 * @param {Map<string, { offset: number, properties: string[] }[]>} parsed
 */
export function assertKeyframesSync(declared, parsed) {
	const declaredNames = new Set(Object.keys(declared));
	const parsedNames = new Set(parsed.keys());

	for (const name of declaredNames) {
		if (!parsedNames.has(name)) {
			throw new Error(`EXPLAINER_DEMO_ANIMATIONS declares @keyframes ${name} but ControlExplainer.svelte has no matching block`);
		}
	}

	for (const name of parsedNames) {
		if (!declaredNames.has(name)) {
			throw new Error(
				`ControlExplainer.svelte defines @keyframes ${name} but EXPLAINER_DEMO_ANIMATIONS has no entry (add it to explainer-demo-keyframes.ts)`
			);
		}
	}

	for (const name of declaredNames) {
		const expectedSteps = declared[name];
		const actualSteps = parsed.get(name);
		assertStepsEqual(name, expectedSteps, actualSteps);
		for (const step of actualSteps) {
			for (const prop of step.properties) {
				if (!ALLOWED.has(prop)) {
					throw new Error(
						`@keyframes ${name} at offset ${step.offset} uses disallowed property "${prop}" (only transform and opacity permitted)`
					);
				}
			}
		}
	}
}

/**
 * @param {string} name
 * @param {readonly { offset: number, properties: readonly string[] }[]} expectedSteps
 * @param {{ offset: number, properties: string[] }[]} actualSteps
 */
function assertStepsEqual(name, expectedSteps, actualSteps) {
	if (expectedSteps.length !== actualSteps.length) {
		throw new Error(
			`@keyframes ${name}: step count mismatch (declared ${expectedSteps.length}, parsed ${actualSteps.length})`
		);
	}
	for (let i = 0; i < expectedSteps.length; i += 1) {
		const exp = expectedSteps[i];
		const act = actualSteps[i];
		if (Math.abs(exp.offset - act.offset) > 0.0001) {
			throw new Error(`@keyframes ${name} step ${i}: offset ${act.offset} !== declared ${exp.offset}`);
		}
		const expProps = [...exp.properties].sort().join(',');
		const actProps = [...act.properties].sort().join(',');
		if (expProps !== actProps) {
			throw new Error(`@keyframes ${name} at offset ${act.offset}: properties [${actProps}] !== declared [${expProps}]`);
		}
	}
}
