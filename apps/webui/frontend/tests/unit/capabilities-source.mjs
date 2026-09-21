import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * The capabilities module's SOURCE-TEXT surface for refusal-sentence drift guards.
 *
 * plan-refusal.test.mjs and store-build-refusal.test.mjs read
 * capabilities.svelte.ts as text to ensure the plan/store-build refusal titles
 * do not collide with daemon capability refusals. An earlier guard counted every
 * module-level single-quoted const and asserted `length >= 4`, which broke
 * whenever a capability was legitimately retired (#3703, commit 1049b0326).
 *
 * Regression lines:
 * - if a *Refusal return inlines a refusal string literal, the guard must fail
 * - if a refusal title equals a capability sentence, the guard must fail
 * - if no exported *Refusal functions are found, the guard must fail (not pass on [])
 */

const CAPABILITIES_PATH = "src/lib/api/capabilities.svelte.ts";
const FRONTEND_ROOT = fileURLToPath(new URL("../..", import.meta.url));

const REFUSAL_FN_RE = /export function (\w+Refusal)\(/g;
const CONST_STRING_RE = /\bconst\s+([A-Z][A-Z0-9_]*)\s*=\s*'([^'\n]+)'/g;
const RETURN_RE = /\breturn\s+([^;]+);/g;

/** Flavor literals in refusal conditions, not refusal sentences themselves. */
const ALLOWED_RETURN_LITERALS = new Set(["engine", "legacy", "unknown"]);

/** Read capabilities.svelte.ts; refuse empty (same contract as engine-source.mjs). */
export function readCapabilitiesSource() {
  const text = readFileSync(`${FRONTEND_ROOT}/${CAPABILITIES_PATH}`, "utf8").replaceAll(
    "\r\n",
    "\n",
  );
  assert.ok(
    text.trim().length > 0,
    `if ${CAPABILITIES_PATH} reads empty this guard asserts nothing`,
  );
  return text;
}

function _extractBlockBody(source, openBraceIndex) {
  let depth = 0;
  for (let i = openBraceIndex; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        const body = source.slice(openBraceIndex + 1, i);
        assert.ok(
          body.trim().length > 0,
          "if a *Refusal function body is empty then the guard asserts nothing",
        );
        return body;
      }
    }
  }
  throw new Error("unbalanced braces in capabilities *Refusal function");
}

function _refusalFunctionBodies(source) {
  const bodies = [];
  for (const match of source.matchAll(REFUSAL_FN_RE)) {
    const name = match[1];
    const sigStart = match.index;
    const openBrace = source.indexOf("{", sigStart);
    assert.notEqual(
      openBrace,
      -1,
      `export function ${name} has no opening brace`,
    );
    bodies.push({ name, body: _extractBlockBody(source, openBrace) });
  }
  return bodies;
}

function _moduleStringConstants(source) {
  const map = new Map();
  for (const match of source.matchAll(CONST_STRING_RE)) {
    map.set(match[1], match[2]);
  }
  return map;
}

function _identifiersFromReturnExpression(fnName, expr) {
  const trimmed = expr.trim();
  if (trimmed === "null") return [];

  for (const match of trimmed.matchAll(/'([^']*)'|"([^"]*)"/g)) {
    const literal = match[1] ?? match[2];
    if (!ALLOWED_RETURN_LITERALS.has(literal)) {
      throw new Error(
        `${fnName} return inlines a string literal instead of a module constant: "${literal}"`,
      );
    }
  }

  if (/^[A-Z][A-Z0-9_]*$/.test(trimmed)) {
    return [trimmed];
  }

  const ternaryArms = trimmed.match(
    /\?\s*([A-Z][A-Z0-9_]*)\s*:\s*([A-Z][A-Z0-9_]*)$/,
  );
  if (ternaryArms !== null) {
    return [ternaryArms[1], ternaryArms[2]];
  }

  throw new Error(
    `${fnName} return must be a module constant identifier or a ternary of identifiers, got: ${trimmed}`,
  );
}

/**
 * Every refusal sentence the capability module can return, derived from exported
 * *Refusal functions whose non-null returns reference module-level const identifiers.
 * Count-agnostic: add or retire a refusal without editing a magic number.
 */
export function capabilityRefusalSentences(source) {
  const constants = _moduleStringConstants(source);
  const functions = _refusalFunctionBodies(source);
  assert.ok(
    functions.length > 0,
    "expected at least one exported *Refusal function in capabilities.svelte.ts",
  );

  const sentences = [];
  for (const { name, body } of functions) {
    for (const match of body.matchAll(RETURN_RE)) {
      const ids = _identifiersFromReturnExpression(name, match[1]);
      for (const id of ids) {
        const sentence = constants.get(id);
        if (sentence === undefined) {
          throw new Error(
            `${name} references undeclared module constant ${id}`,
          );
        }
        sentences.push(sentence);
      }
    }
  }

  return [...new Set(sentences)];
}

/**
 * Assert the structural property the old `>= 4` count was a brittle proxy for.
 * Throws with a clear message naming the offending function or return.
 */
export function assertCapabilityRefusalsAreModuleConstants(source) {
  capabilityRefusalSentences(source);
}

/**
 * Runs assertCapabilityRefusalsAreModuleConstants, then asserts refusalTitle is
 * not among the capability refusal sentences.
 */
export function assertRefusalDistinctFromCapabilityRefusals(source, refusalTitle) {
  assertCapabilityRefusalsAreModuleConstants(source);
  const sentences = capabilityRefusalSentences(source);
  assert.ok(
    !sentences.includes(refusalTitle),
    "the refusal title has collided with a capability refusal",
  );
}
