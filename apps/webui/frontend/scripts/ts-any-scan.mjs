// ts-any-scan.mjs -- find every explicit `any` and compiler-suppression
// directive by parsing real syntax, never by regex.
//
// WHY THIS EXISTS
// scripts/quality_gate.py used to grep for `any` with five token-adjacency
// regexes (assertion, annotation, alias, type-argument, operator/keyword
// position). Codex review of PR #731 found two rounds of gaps in that
// approach: `type X = [any]`, `interface X { values: readonly any[] }` and
// `type X = (any)` all landed green, and the same regexes fired inside
// string/template literals and trailing comments (`"cast it as any"`,
// `// Returns: any value`). Token adjacency cannot enumerate every syntactic
// position `any` can occupy. A real parse walks every position at once and
// structurally cannot see inside a string or a comment, because neither
// tokenizes as a keyword.
//
// A third round found two more gaps in the same family:
// - the directive regexes (`@ts-ignore` etc.) still ran over raw lines, so
//   `const help = "Never use @ts-ignore here";` counted as a suppression.
//   Directives are only ever legal inside a comment, so they are now found
//   by tokenizing and keeping only comment trivia, the same fix already
//   proven for `any`.
// - `.svelte` extraction only ever read `<script>` block text, so an `any`
//   cast written directly in markup (`on:click={(e) => e.target as any}`,
//   a pattern already live in the tree) scored as clean. `.svelte` files are
//   now parsed with `svelte/compiler` itself, which returns one AST covering
//   both script blocks and every markup expression, so the same
//   `TSAnyKeyword` walk covers all of it in one pass with no separate
//   extraction step to miss a block or a location.
//
// USAGE
//   node ts-any-scan.mjs <manifest.json>
// manifest.json is a JSON array of absolute file paths (.ts/.js/.svelte).
// Prints one JSON object to stdout on success:
//   { filesScanned, svelteFilesTotal, svelteFilesWithScript,
//     hits: [{file, line, rule}] }
// rule is one of "any", "ts-ignore", "ts-expect-error", "ts-nocheck".
// Exits 1 with a message on stderr if any file fails to parse. A parse
// failure must never render as zero hits -- that would be reporting a
// failed measurement as a clean tree.

import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { parse as parseSvelte } from 'svelte/compiler';

function _fail(message) {
  console.error(`[ERROR] ${message}`);
  process.exit(1);
}

// (line, rule) for every suppression directive found by a real parse of
// `text`. `lineOffset` shifts every hit for text sliced out of a larger file
// (a .svelte script block); 0 for a whole standalone file.
//
// A prior version hand-rolled this with `ts.createScanner` plus three
// regexes replicating TypeScript's own directive/pragma grammar. Round
// thirteen of PR #731 review (Codex, P2/BLOCKING) found the reason that
// approach cannot work in general: a context-free scanner call is never
// driven through `reScanTemplateToken`, so after the `}` closing a
// `${...}` interpolation it free-runs the template's tail text as a fresh
// token stream -- `` `Never write ${1} // @ts-ignore here` `` scanned as a
// real `SingleLineCommentTrivia` containing a directive, verified against
// pinned tsc 5.9.3 to suppress nothing at all.
//
// The parser's own scanner does not have this gap, because
// `reScanTemplateToken` only exists inside the parser's incremental-lexing
// loop. `ts.createSourceFile` already runs that parse and records every
// comment directive and pragma it recognizes on the SourceFile itself
// (`sourceFile.commentDirectives`, `sourceFile.pragmas`), verified against
// pinned tsc 5.9.3 to reproduce every case this file used to hand-derive --
// case sensitivity, slash-count/suffix tolerance on ts-ignore/expect-error,
// the block-comment final-line and prefix rules, and every ts-nocheck
// placement nuance -- for free, including the template-tail case, and it
// stays error-tolerant: a slice with a real parse error still yields correct
// directives, because they are recorded during scanning, not after a
// successful parse.
// tsc's own directive grammars (pinned in tests/quality/
// test_frontend_typing_directives.py). Applied only to comment trivia the
// parser already classified, never to raw lines, so string/template prose
// cannot score.
const _SL_DIRECTIVE_RE = /^\/\/\/?\s*@(ts-expect-error|ts-ignore)/;
const _ML_DIRECTIVE_RE = /^[/*]+\s*@(ts-expect-error|ts-ignore)/;
const _SL_PRAGMA_RE = /^\s*\/\/\/?\s*@([^\s:]+)((?:[^\S\r\n]|:).*)?$/;

function _pragmaNocheckName(name) {
  return /^ts-nocheck$/i.test(name);
}

function _ruleFromSingleLineComment(comment) {
  const m = comment.match(_SL_DIRECTIVE_RE);
  return m ? m[1] : null;
}

function _ruleFromBlockComment(comment) {
  const finalLine = comment.split(/\r?\n/).pop() ?? '';
  const trimmed = finalLine.trimStart();
  const prefixed = trimmed.match(_ML_DIRECTIVE_RE);
  if (prefixed) return prefixed[1];
  const bare = trimmed.match(/^@(ts-expect-error|ts-ignore)/);
  return bare ? bare[1] : null;
}

function _forEachRealComment(sourceFile, text, cb) {
  const seen = new Set();
  const record = (range, commentText) => {
    if (seen.has(range.pos)) return;
    seen.add(range.pos);
    cb(range, commentText);
  };
  const walk = (node) => {
    const leading = ts.getLeadingCommentRanges(text, node.getFullStart());
    if (leading) {
      for (const range of leading) record(range, text.slice(range.pos, range.end));
    }
    const trailing = ts.getTrailingCommentRanges(text, node.end);
    if (trailing) {
      for (const range of trailing) record(range, text.slice(range.pos, range.end));
    }
    ts.forEachChild(node, walk);
  };
  walk(sourceFile);
  ts.forEachLeadingCommentRange(text, 0, (pos, end) => {
    record({ pos, end }, text.slice(pos, end));
  });
}

function _fileLeadingNocheck(text, lineOffset) {
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '') continue;
    const m = line.match(_SL_PRAGMA_RE);
    if (!m) return null;
    if (!_pragmaNocheckName(m[1])) return null;
    return { line: i + 1 + lineOffset, rule: 'ts-nocheck' };
  }
  return null;
}

function _directiveHits(text, lineOffset) {
  const sourceFile = ts.createSourceFile('probe.ts', text, ts.ScriptTarget.Latest, true);
  const hits = [];
  const seen = new Set();
  const add = (line, rule) => {
    const key = `${line}:${rule}`;
    if (seen.has(key)) return;
    seen.add(key);
    hits.push({ line, rule });
  };

  for (const directive of sourceFile.commentDirectives ?? []) {
    const line = sourceFile.getLineAndCharacterOfPosition(directive.range.pos).line + 1 + lineOffset;
    const rule = directive.type === ts.CommentDirectiveType.ExpectError ? 'ts-expect-error' : 'ts-ignore';
    add(line, rule);
  }

  _forEachRealComment(sourceFile, text, (range, commentText) => {
    // Which POSITION names the hit's line matters, because this loop and the
    // `commentDirectives` loop above can both find the same directive and
    // `add()` de-dups on `line:rule`. A block comment's directive can only sit
    // on its FINAL physical line -- that is all `_ruleFromBlockComment` looks
    // at, mirroring tsc's own `lastLineStart` behavior -- and tsc reports that
    // same final line. Reporting `range.pos` here named the line the comment
    // OPENED on instead, so the two paths disagreed by the comment's height and
    // a multi-line block comment scored twice, once per path.
    let directivePos = range.pos;
    let rule = null;
    if (commentText.startsWith('//')) {
      rule = _ruleFromSingleLineComment(commentText);
    } else if (commentText.startsWith('/*')) {
      rule = _ruleFromBlockComment(commentText);
      directivePos = range.end;
    }
    if (rule) {
      add(sourceFile.getLineAndCharacterOfPosition(directivePos).line + 1 + lineOffset, rule);
    }
  });

  const nocheckEntries = sourceFile.pragmas?.get?.('ts-nocheck');
  const nocheckList = nocheckEntries == null ? [] : Array.isArray(nocheckEntries) ? nocheckEntries : [nocheckEntries];
  for (const entry of nocheckList) {
    const line = sourceFile.getLineAndCharacterOfPosition(entry.range.pos).line + 1 + lineOffset;
    add(line, 'ts-nocheck');
  }
  const leading = _fileLeadingNocheck(text, lineOffset);
  if (leading) add(leading.line, leading.rule);

  return hits;
}

// (line) for every `AnyKeyword` node found by a real parse of `text`.
// Returns {error} instead of throwing so the caller can report which file
// and fail the whole run rather than rendering a failed parse as zero hits.
//
// `ts.forEachChild` does not descend into a node's `.jsDoc` array -- a
// documented TypeScript-compiler-API trap, verified directly against pinned
// tsc: parsing `// @ts-check\n/** @type {any} */\nlet x;` produces a real
// `AnyKeyword` node reachable via `node.jsDoc[0].tags[0].typeExpression`,
// but a plain `forEachChild` walk from the root never visits it. The
// project scan includes `.js` files, where a JSDoc `@type {any}` is the
// only way to spell this escape hatch, so `walk` must visit `node.jsDoc`
// explicitly.
//
// JSDoc's own wildcard spelling, `@type {*}`, is a SEPARATE node kind,
// `JSDocAllType`, never `AnyKeyword` (round fourteen of PR #731 review,
// Codex P1/BLOCKING) -- verified against pinned tsc 5.9.3: a checked .js
// file typed this way lets a nonexistent property access through with no
// error, same as `any`, but the walk above only ever checked `AnyKeyword`,
// so it reached the node (jsDoc is already walked) and never recorded it.
// A BARE `@type {?}` (no type after the `?`) is a third any-like spelling,
// `JSDocUnknownType` (round fifteen, Codex P1/BLOCKING) -- verified the
// same way: the nonexistent-property probe still passes with no error.
// This is NOT the same node as `JSDocNullableType` (`?string`, a question
// mark immediately followed by a real type, meaning "string or null") --
// confirmed by dumping the raw parse tree -- and that form genuinely is a
// narrow real type: verified the probe DOES still error on it, so it must
// stay unflagged.
function _anyHits(text, lineOffset, scriptKind) {
  const sourceFile = ts.createSourceFile('probe.ts', text, ts.ScriptTarget.Latest, true, scriptKind);
  if (sourceFile.parseDiagnostics.length > 0) {
    return { error: sourceFile.parseDiagnostics.map((d) => ts.flattenDiagnosticMessageText(d.messageText, ' ')).join('; ') };
  }
  const ANY_LIKE_KINDS = new Set([ts.SyntaxKind.AnyKeyword, ts.SyntaxKind.JSDocAllType, ts.SyntaxKind.JSDocUnknownType]);
  const lines = [];
  const walk = (node) => {
    if (ANY_LIKE_KINDS.has(node.kind)) {
      lines.push(sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1 + lineOffset);
    }
    if (node.jsDoc) {
      for (const doc of node.jsDoc) walk(doc);
    }
    ts.forEachChild(node, walk);
  };
  walk(sourceFile);
  return { lines };
}

// Every `TSAnyKeyword` node anywhere in a svelte/compiler AST subtree --
// script instance, script module or markup fragment alike, since all three
// come back as the same TS-flavored ESTree shape from one `parse()` call.
function _walkForAnyKeyword(node, lines) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'TSAnyKeyword') {
    lines.push(node.loc.start.line);
    return;
  }
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end') continue;
    const value = node[key];
    if (Array.isArray(value)) {
      for (const item of value) _walkForAnyKeyword(item, lines);
    } else if (value && typeof value === 'object') {
      _walkForAnyKeyword(value, lines);
    }
  }
}

// `<script lang="ts" generics="T extends any">` is checked TypeScript that
// svelte-check compiles into a real generic parameter list, but it lives as
// a plain attribute string, never inside `content` (an ESTree Program), so
// the walk above cannot see it. Wrapping it as a throwaway generic function
// signature and running it through the same parser as every other `.ts`
// probe in this file reuses `_anyHits` instead of a second detection path.
function _genericsAnyLines(raw, block) {
  const attr = (block.attributes ?? []).find((a) => a.name === 'generics');
  if (!attr) return [];
  const text = (attr.value ?? []).map((v) => v.data ?? '').join('');
  if (!text) return [];
  const lineOffset = raw.slice(0, attr.value[0].start).split('\n').length - 1;
  const { lines, error } = _anyHits(`function _f<${text}>() {}`, lineOffset, ts.ScriptKind.TS);
  if (error) {
    _fail(`generics="${text}" failed to parse as TypeScript: ${error}`);
  }
  return lines;
}

// svelte/compiler's own ESTree AST (walked by `_walkForAnyKeyword` above)
// carries comments as raw text, never as JSDoc nodes -- it is not what
// resolves `/** @type {any} */` into an `AnyKeyword`. Running the block's
// own raw text back through `_anyHits`, the real TS parser already fixed
// to walk `.jsDoc` (round ten of PR #731 review), is what actually finds
// it -- verified structurally the same way as the standalone `.js` case.
// This duplicates any ordinary (non-JSDoc) `any` the ESTree walk already
// found at the same line, which is harmless for a hard-zero metric: a
// duplicate of a real violation is still a real violation, and the live
// tree carries zero either way.
function _jsDocAnyLines(raw, block) {
  const { start, end } = block.content;
  const lineOffset = raw.slice(0, start).split('\n').length - 1;
  const { lines, error } = _anyHits(raw.slice(start, end), lineOffset, ts.ScriptKind.TS);
  if (error) {
    _fail(`svelte script block failed to parse as TypeScript: ${error}`);
  }
  return lines;
}

// A .svelte file is one component-wide grammar: script blocks (instance,
// and an optional `context="module"` block) plus every TS expression
// embedded in markup. svelte/compiler parses all of it in one AST, which is
// why this replaces the old regex-based <script>-only extraction outright
// rather than adding a second pass for markup.
function _svelteHits(raw, file) {
  let root;
  try {
    root = parseSvelte(raw, { filename: file, modern: true });
  } catch (error) {
    return { error: error.message, hasScript: false };
  }
  const hasScript = Boolean(root.instance || root.module);

  const anyLines = [];
  _walkForAnyKeyword(root.fragment, anyLines);
  if (root.instance) {
    _walkForAnyKeyword(root.instance.content, anyLines);
    anyLines.push(..._genericsAnyLines(raw, root.instance));
    anyLines.push(..._jsDocAnyLines(raw, root.instance));
  }
  if (root.module) {
    _walkForAnyKeyword(root.module.content, anyLines);
    anyLines.push(..._genericsAnyLines(raw, root.module));
    anyLines.push(..._jsDocAnyLines(raw, root.module));
  }
  const hits = anyLines.map((line) => ({ line, rule: 'any' }));

  // Directives are only meaningful inside a <script> block: scanning full
  // markup with a TS lexer would misread ordinary text (a `//` in an href,
  // for instance) as comment trivia. Slicing to the parser's own
  // instance/module code range, rather than a hand-rolled <script> regex,
  // is what makes this correct across attribute variants and block order.
  for (const block of [root.instance, root.module]) {
    if (!block) continue;
    const { start, end } = block.content;
    const lineOffset = raw.slice(0, start).split('\n').length - 1;
    hits.push(..._directiveHits(raw.slice(start, end), lineOffset));
  }

  return { hits, hasScript };
}

function main() {
  const manifestPath = process.argv[2];
  if (!manifestPath) _fail('usage: node ts-any-scan.mjs <manifest.json>');

  let files;
  try {
    files = JSON.parse(readFileSync(manifestPath, 'utf8'));
  } catch (error) {
    _fail(`manifest ${manifestPath} is not valid JSON: ${error.message}`);
  }
  if (!Array.isArray(files) || files.length === 0) {
    _fail('manifest listed no files; that is a broken caller reporting as a clean scan');
  }

  const hits = [];
  let svelteFilesTotal = 0;
  let svelteFilesWithScript = 0;
  let filesScanned = 0;

  for (const file of files) {
    let raw;
    try {
      raw = readFileSync(file, 'utf8');
    } catch (error) {
      _fail(`could not read ${file}: ${error.message}`);
    }

    if (file.endsWith('.svelte')) {
      svelteFilesTotal += 1;
      const result = _svelteHits(raw, file);
      if (result.error) {
        _fail(`${file} failed to parse as Svelte: ${result.error}`);
      }
      if (result.hasScript) svelteFilesWithScript += 1;
      for (const hit of result.hits) hits.push({ file, ...hit });
    } else {
      const scriptKind = file.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS;
      const { lines, error } = _anyHits(raw, 0, scriptKind);
      if (error) {
        _fail(`${file} failed to parse as TypeScript: ${error}`);
      }
      for (const line of lines) hits.push({ file, line, rule: 'any' });
      for (const hit of _directiveHits(raw, 0)) hits.push({ file, ...hit });
    }
    filesScanned += 1;
  }

  console.log(JSON.stringify({ filesScanned, svelteFilesTotal, svelteFilesWithScript, hits }));
}

main();
