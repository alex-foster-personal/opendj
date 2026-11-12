/**
 * Ratchet: a unit test that reads a `.svelte` component as TEXT and never
 * renders one needs an allowlist entry with a reason (GUARD-15).
 *
 * Why. the maintainer, Wed 7 Oct 2026, on the UI-test gap scan: "I assume we're adding
 * them when we need to but maybe we're not?" The scan found about a third of
 * this suite regexing component source. A regex over `Foo.svelte` proves a
 * string is in the file; it cannot prove the component mounts, that an
 * `{#if}` gates on the state it claims to, or what markup a user sees. Those
 * tests stay (they are cheap drift pins), but NEW ones must either render the
 * component (see RENDERED_EXAMPLE below: `loadSvelteSsrModule` + svelte's own
 * `render` from `svelte/server`) or carry an allowlist entry saying why text is
 * the right instrument. The allowlist may only shrink: an entry whose file no
 * longer matches (deleted, or converted to a rendered test) fails as stale.
 *
 * Allowlist: tests/unit/source-regex-test-allowlist.json, `{ file, reason }`
 * per entry, `file` relative to tests/unit. Seeded Wed 7 Oct 2026 with every
 * offender on main at that time, reason "pre-existing (ratchet seed 2026-10-07)".
 *
 * THE HEURISTIC (a lint, not a proof), per tests/unit/**\/*.test.mjs:
 *
 *   READS SOURCE when the file can read text, i.e. calls fs `readFile` /
 *   `readFileSync` (also under an alias bound by `import { readFileSync as
 *   read } from 'node:fs'` or from `node:fs/promises`) or imports a sibling
 *   reader helper (a non-test .mjs in tests/unit that calls readFile, render
 *   helpers excluded; today engine-source.mjs `readFrontendSource`), AND
 *   names a real component: a string or template literal ending in
 *   `<name>.svelte` whose `<name>` is the basename of a `.svelte` file under
 *   src/, or a bare `'.svelte'` extension literal (a scan over all of them).
 *   So `Foo.svelte.ts`, a rune-module alias such as `'$lib/stores.svelte'`
 *   and the prose possessive `Foo.svelte's` do not count. Also READS SOURCE:
 *   importing a sibling helper that itself names a component (today
 *   contrast.mjs, which reads TrackTable.svelte for a CSS backdrop).
 *
 *   RENDERS when the file CALLS (not merely imports) one of the render entry
 *   points: `loadSvelteSsrModule` (load-svelte-ssr.mjs), `renderToHtml` or
 *   `bundleSvelteEntry` (mount-svelte.mjs), or `render`/`mount` bound from
 *   `svelte/server` / `svelte` by a static or dynamic import, or a client
 *   bundler from mount-svelte-client.mjs followed by a member `mount` call.
 *
 *   The file is PARSED (TypeScript compiler, JS mode), not regexed, so text
 *   in comments, strings and regex literals is never a call, and relative
 *   imports resolve against the test's own directory (a test under slow/
 *   importing '../engine-source.mjs' is seen).
 *
 *   OFFENDER = READS SOURCE and not RENDERS.
 *
 * LIMITS, stated so nobody reads more into a green run than is there:
 *
 *   - A file that renders ONE component and regexes ANOTHER is not flagged.
 *     The rule is per file, not per assertion: "renders something" is the bar.
 *   - A file can call a render helper and then regex the source anyway (for
 *     example call `loadSvelteSsrModule` on an entry with no component and
 *     ignore the result). Calling the helper is cheaper than lying about it,
 *     but nothing here checks the rendered output is what gets asserted on.
 *     Review owns that.
 *   - Source reached by indirection the scan cannot see is missed: a path
 *     built without a `.svelte` literal (e.g. `name + ext`), a helper two
 *     imports deep, or a `child_process` reading the file.
 *   - Read and literal are only co-located, not data-flow linked: a file that
 *     reads a JSON fixture and names a component path for another reason is
 *     flagged though it never reads the component. The allowlist absorbs
 *     that, with a reason saying so.
 *   - Only `.svelte` components are in scope. `.svelte.ts` rune modules have
 *     no template; reading them as text is a different (and smaller) problem.
 */
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const UNIT_DIR = fileURLToPath(new URL(".", import.meta.url));
const SELF = fileURLToPath(import.meta.url);
const ALLOWLIST_PATH = join(UNIT_DIR, "source-regex-test-allowlist.json");
const SEED_REASON = "pre-existing (ratchet seed 2026-10-07)";
/** A test that really renders, to point new authors at. Checked below. */
const RENDERED_EXAMPLE = "setup-overlay-render.test.mjs";
/** A seeded offender the detector must find (positive control). */
const KNOWN_OFFENDER = "head-delay-stepper.test.mjs";
/** Helpers that read `.svelte` only to COMPILE and render it. */
const RENDER_HELPERS = new Set([
  "load-svelte-ssr.mjs",
  "mount-svelte.mjs",
  "mount-svelte-client.mjs",
]);

const SRC_DIR = fileURLToPath(new URL("../../src", import.meta.url));
const FS_READERS = ["readFile", "readFileSync"];
/** Where a synthetic control "lives" when no file path is given. */
const SYNTHETIC_FILE = join(UNIT_DIR, "synthetic.test.mjs");

/** Basenames (without extension) of every real `.svelte` component under src/. */
function componentNames(dir = SRC_DIR, out = new Set()) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) componentNames(p, out);
    else if (entry.name.endsWith(".svelte"))
      out.add(entry.name.slice(0, -".svelte".length));
  }
  return out;
}
const COMPONENTS = componentNames();

/**
 * Parse with the TypeScript compiler (already a devDependency, and the parser
 * raf-non-visual-lint.test.mjs uses) so comments, string contents and regex
 * literals are never mistaken for calls. Returns the file's imports (module
 * specifier resolved against the file, local name -> imported name), the
 * names it calls directly (`foo(`), the member names it calls (`x.foo(`),
 * and the text of every string and template literal piece.
 */
function scan(source, file) {
  const sf = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    false,
    ts.ScriptKind.JS,
  );
  const imports = [];
  const calls = new Set();
  const memberCalls = new Set();
  /** `ns.name` for every call whose callee is `<identifier>.<name>(`. */
  const qualifiedCalls = new Set();
  const literals = [];
  const from = (spec) =>
    spec.startsWith(".")
      ? resolve(dirname(file), spec)
      : spec.replace(/^node:/, "");
  const isImportCall = (n) =>
    ts.isCallExpression(n) &&
    n.expression.kind === ts.SyntaxKind.ImportKeyword &&
    n.arguments.length > 0 &&
    ts.isStringLiteralLike(n.arguments[0]);
  const visit = (node) => {
    if (
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const names = new Map();
      let namespace = null;
      const nb = node.importClause?.namedBindings;
      if (nb && ts.isNamedImports(nb)) {
        for (const el of nb.elements)
          names.set(el.name.text, (el.propertyName ?? el.name).text);
      } else if (nb && ts.isNamespaceImport(nb)) {
        namespace = nb.name.text;
      }
      imports.push({
        module: from(node.moduleSpecifier.text),
        names,
        namespace,
      });
      return; // a module specifier is not a path the test reads
    }
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isObjectBindingPattern(node.name)
    ) {
      const init = ts.isAwaitExpression(node.initializer)
        ? node.initializer.expression
        : node.initializer;
      if (isImportCall(init)) {
        const names = new Map();
        for (const el of node.name.elements) {
          if (!ts.isIdentifier(el.name)) continue;
          const orig =
            el.propertyName && ts.isIdentifier(el.propertyName)
              ? el.propertyName
              : el.name;
          names.set(el.name.text, orig.text);
        }
        imports.push({
          module: from(init.arguments[0].text),
          names,
          namespace: null,
        });
      }
    }
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isIdentifier(node.name)
    ) {
      // `const server = await import('svelte/server')`: a namespace binding.
      const init = ts.isAwaitExpression(node.initializer)
        ? node.initializer.expression
        : node.initializer;
      if (isImportCall(init)) {
        imports.push({
          module: from(init.arguments[0].text),
          names: new Map(),
          namespace: node.name.text,
        });
      }
    }
    if (isImportCall(node)) {
      imports.push({
        module: from(node.arguments[0].text),
        names: new Map(),
        namespace: null,
      });
      return;
    }
    if (ts.isCallExpression(node)) {
      if (ts.isIdentifier(node.expression)) calls.add(node.expression.text);
      else if (ts.isPropertyAccessExpression(node.expression)) {
        memberCalls.add(node.expression.name.text);
        if (ts.isIdentifier(node.expression.expression)) {
          qualifiedCalls.add(
            `${node.expression.expression.text}.${node.expression.name.text}`,
          );
        }
      }
    }
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    ) {
      literals.push(node.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return { imports, calls, memberCalls, qualifiedCalls, literals };
}

/** Local names bound to any of `names` by an import of one of `modules`. */
function boundFrom(scanned, modules, names) {
  const out = new Set();
  for (const imp of scanned.imports) {
    if (!modules.includes(imp.module)) continue;
    for (const [local, orig] of imp.names)
      if (names.includes(orig)) out.add(local);
  }
  return out;
}

const importsModule = (scanned, absPaths) =>
  scanned.imports.some((imp) => absPaths.includes(imp.module));
const calledAny = (scanned, locals) =>
  [...locals].some((n) => scanned.calls.has(n));

/** True when one of `names` from one of `modules` is CALLED: by a named
 * binding (`render(`, aliased or not) or through a namespace binding
 * (`import * as server from 'svelte/server'; server.render(`). */
function calledFrom(scanned, modules, names) {
  if (calledAny(scanned, boundFrom(scanned, modules, names))) return true;
  return scanned.imports.some(
    (imp) =>
      imp.namespace !== null &&
      modules.includes(imp.module) &&
      names.some((n) => scanned.qualifiedCalls.has(`${imp.namespace}.${n}`)),
  );
}

/**
 * True when the file calls an fs reader: `readFileSync(` / `fs.readFileSync(` /
 * `fs.promises.readFile(` by name, or a local alias bound from `fs` or
 * `fs/promises` (`import { readFileSync as read } from 'node:fs'`).
 */
function callsFsReader(scanned) {
  if (
    FS_READERS.some((n) => scanned.calls.has(n) || scanned.memberCalls.has(n))
  )
    return true;
  return calledFrom(scanned, ["fs", "fs/promises"], FS_READERS);
}

/**
 * True when a string or template literal names a REAL component by a path
 * ending in `<name>.svelte`. `'$lib/stores.svelte'` resolves to
 * `stores.svelte.ts`, which has no template, so it does not count; neither do
 * `Foo.svelte.ts` or the prose `Foo.svelte's`, which do not END in `.svelte`.
 * A bare `'.svelte'` literal (a scan filtering on the extension) counts as
 * naming all of them.
 */
function namesComponent(scanned, components) {
  for (const text of scanned.literals) {
    if (text === ".svelte") return true;
    const m = /([\w+-]+)\.svelte$/.exec(text);
    if (m && components.has(m[1])) return true;
  }
  return false;
}

/** Helper names may be absolute (from the scan) or relative to tests/unit (controls). */
const unitPaths = (names) =>
  [...names].map((n) => (isAbsolute(n) ? n : join(UNIT_DIR, n)));

/**
 * Helper modules (non-test .mjs anywhere under tests/unit, fixtures and
 * node_modules skipped, the two render helpers excluded) that call an fs
 * reader. Kept as ABSOLUTE paths, so a test's import resolves to exactly
 * one helper whether it sits beside the test or a level up. `readers` is every such helper (a test
 * that imports one and names a component path is reading it through the
 * helper: engine-source.mjs `readFrontendSource`); `componentReaders` is the
 * subset that names a component itself (contrast.mjs reads TrackTable.svelte).
 */
export function sourceReadingHelpers(dir = UNIT_DIR, components = COMPONENTS) {
  const readers = new Set();
  const componentReaders = new Set();
  const walk = (d) => {
    for (const entry of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "fixtures" && entry.name !== "node_modules") walk(p);
        continue;
      }
      if (!entry.name.endsWith(".mjs") || entry.name.endsWith(".test.mjs"))
        continue;
      if (d === dir && RENDER_HELPERS.has(entry.name)) continue;
      const scanned = scan(readFileSync(p, "utf8"), p);
      if (!callsFsReader(scanned)) continue;
      readers.add(p);
      if (namesComponent(scanned, components)) componentReaders.add(p);
    }
  };
  walk(dir);
  return { readers, componentReaders };
}

function rendersScanned(scanned) {
  return (
    calledFrom(scanned, unitPaths(["load-svelte-ssr.mjs"]), [
      "loadSvelteSsrModule",
    ]) ||
    calledFrom(scanned, unitPaths(["mount-svelte.mjs"]), [
      "renderToHtml",
      "bundleSvelteEntry",
    ]) ||
    (calledFrom(scanned, unitPaths(["mount-svelte-client.mjs"]), [
      "bundlePreflightCheckRowClient",
      "bundleStemWaveRowViewClient",
      "bundleRecorderStatusRetryClient",
    ]) && scanned.memberCalls.has("mount")) ||
    calledFrom(scanned, ["svelte/server"], ["render"]) ||
    calledFrom(scanned, ["svelte"], ["mount"])
  );
}

export function rendersComponent(source, file = SYNTHETIC_FILE) {
  return rendersScanned(scan(source, file));
}

export function classify(
  source,
  helpers,
  components = COMPONENTS,
  file = SYNTHETIC_FILE,
) {
  const scanned = scan(source, file);
  const canRead =
    callsFsReader(scanned) ||
    importsModule(scanned, unitPaths(helpers.readers));
  const reads =
    (canRead && namesComponent(scanned, components)) ||
    importsModule(scanned, unitPaths(helpers.componentReaders));
  const renders = rendersScanned(scanned);
  return { reads, renders, offender: reads && !renders };
}

function walkTests(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "fixtures" || entry.name === "node_modules") continue;
      out.push(...walkTests(p));
    } else if (entry.name.endsWith(".test.mjs") && p !== SELF) {
      // SELF is excluded explicitly: it lists src/**/*.svelte to know the
      // component names, and its synthetic fixtures contain render calls
      // as strings, so classifying it would answer nothing either way.
      out.push(p);
    }
  }
  return out;
}

export function findOffenders(dir = UNIT_DIR) {
  const helpers = sourceReadingHelpers(dir);
  return walkTests(dir)
    .filter(
      (p) => classify(readFileSync(p, "utf8"), helpers, COMPONENTS, p).offender,
    )
    .map((p) => relative(dir, p).split("\\").join("/"))
    .sort();
}

function loadAllowlist() {
  const parsed = JSON.parse(readFileSync(ALLOWLIST_PATH, "utf8"));
  assert.ok(
    Array.isArray(parsed.entries),
    "allowlist must have an entries array",
  );
  return parsed.entries;
}

const offenders = findOffenders();
const allowlist = loadAllowlist();

test("control: the detector finds the seeded offenders, including a known one", () => {
  assert.ok(
    offenders.length > 0,
    "detector found zero offenders; the instrument is broken",
  );
  assert.ok(
    offenders.includes(KNOWN_OFFENDER),
    `${KNOWN_OFFENDER} reads HeadphoneCluster.svelte as text and never renders; it must be detected`,
  );
  assert.ok(
    allowlist.filter((e) => e.reason === SEED_REASON).length > 0,
    "seeded entries are missing from the allowlist",
  );
});

test("control: a known rendered test is NOT flagged", () => {
  const src = readFileSync(join(UNIT_DIR, RENDERED_EXAMPLE), "utf8");
  assert.ok(
    rendersComponent(src, join(UNIT_DIR, RENDERED_EXAMPLE)),
    `${RENDERED_EXAMPLE} must classify as rendering`,
  );
  assert.ok(!offenders.includes(RENDERED_EXAMPLE));
});

test("control: the classifier says yes and no on synthetic inputs", () => {
  const none = { readers: new Set(), componentReaders: new Set() };
  const comps = new Set(["Foo"]);
  const cls = (src, h = none) => classify(src, h, comps);
  const reader = `import { readFileSync } from 'node:fs';\nconst s = readFileSync('src/lib/Foo.svelte', 'utf8');\nassert.match(s, /x/);`;
  assert.equal(
    cls(reader).offender,
    true,
    "plain source regex must be flagged",
  );
  // Importing a renderer without calling it is the cheapest bypass; it must not count.
  const importOnly = `import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';\n${reader}`;
  assert.equal(
    cls(importOnly).offender,
    true,
    "import-only renderer must still be flagged",
  );
  const commentCall = `${importOnly}\n// loadSvelteSsrModule(ENTRY)`;
  assert.equal(
    cls(commentCall).offender,
    true,
    "a call in a comment must not count",
  );
  const rendered = `${importOnly}\nconst mod = await loadSvelteSsrModule(ENTRY);`;
  assert.equal(
    cls(rendered).offender,
    false,
    "a real render call clears the file",
  );
  const destructured = `${importOnly}\nconst { render, Foo } = await loadSvelteSsrModule(ENTRY);`;
  assert.equal(
    cls(destructured).offender,
    false,
    "a destructuring render call clears the file",
  );
  const aliased = `import { loadSvelteSsrModule as load } from './load-svelte-ssr.mjs';\n${reader}\nawait load(ENTRY);`;
  assert.equal(
    cls(aliased).offender,
    false,
    "an aliased render helper call clears the file",
  );
  const dyn = `${reader}\nconst { render } = await import('svelte/server');\nrender(C);`;
  assert.equal(
    cls(dyn).offender,
    false,
    "dynamic svelte/server render clears the file",
  );
  const mounted = `import { mount } from 'svelte';\n${reader}\nmount(Foo, { target });`;
  assert.equal(cls(mounted).offender, false, "svelte mount clears the file");
  const runeOnly = `import { readFileSync } from 'node:fs';\nreadFileSync('src/lib/x.svelte.ts', 'utf8');\nconst a = { '$lib/stores.svelte': X };\n// Foo.svelte's order()`;
  assert.equal(
    cls(runeOnly).reads,
    false,
    ".svelte.ts, rune-module aliases and prose are out of scope",
  );
  const possessive = `import { readFileSync } from 'node:fs';\nreadFileSync('fixture.json', 'utf8');\nassert.ok(x, \`Foo.svelte's slider moved\`);`;
  assert.equal(
    cls(possessive).reads,
    false,
    "a possessive Foo.svelte's in a message is not a path",
  );
  const notAComponent = `import { readFileSync } from 'node:fs';\nreadFileSync('src/lib/Bar.svelte', 'utf8');`;
  assert.equal(
    cls(notAComponent).reads,
    false,
    "a literal naming no real component does not count",
  );
  const aliasedSync = `import { readFileSync as read } from 'node:fs';\nconst s = read('src/lib/Foo.svelte', 'utf8');`;
  assert.equal(
    cls(aliasedSync).offender,
    true,
    "an aliased readFileSync must be flagged (#5857 Codex P2)",
  );
  const aliasedAsync = `import { readFile as rf } from 'node:fs/promises';\nconst s = await rf('src/lib/Foo.svelte', 'utf8');`;
  assert.equal(
    cls(aliasedAsync).offender,
    true,
    "an aliased fs/promises readFile must be flagged",
  );
  const dynAlias = `const { readFileSync: rd } = await import('fs');\nrd('src/lib/Foo.svelte', 'utf8');`;
  assert.equal(
    cls(dynAlias).offender,
    true,
    "a dynamically imported alias must be flagged",
  );
  // Opposite direction: an alias from a module that is not fs is not a reader.
  const notFs = `import { readFileSync as read } from './fixtures.mjs';\nread('src/lib/Foo.svelte');`;
  assert.equal(
    cls(notFs).reads,
    false,
    "a readFileSync alias from a non-fs module does not count",
  );
  const importedNotCalled = `import { readFileSync as read } from 'node:fs';\nconst p = 'src/lib/Foo.svelte';`;
  assert.equal(
    cls(importedNotCalled).reads,
    false,
    "an aliased reader that is never called does not read",
  );
  const scanAll = `import { readFileSync } from 'node:fs';\nfiles.filter((p) => p.endsWith('.svelte')).map((p) => readFileSync(p, 'utf8'));`;
  assert.equal(
    cls(scanAll).offender,
    true,
    "a scan over every .svelte file must be flagged",
  );
  const viaReader = `import { readFrontendSource } from './engine-source.mjs';\nreadFrontendSource('src/lib/Foo.svelte');`;
  assert.equal(
    cls(viaReader, {
      readers: new Set(["engine-source.mjs"]),
      componentReaders: new Set(),
    }).offender,
    true,
    "a read through a sibling reader helper must be flagged",
  );
  // Text that only LOOKS like a call is not one (#5857 Codex P2).
  const renderImported = `import { render } from 'svelte/server';\n${reader}`;
  const trailingComment = `${renderImported}\nassert.ok(s); // render(NotAComponent)`;
  assert.equal(
    cls(trailingComment).offender,
    true,
    "a render call in a trailing comment must not count",
  );
  const inString = `${renderImported}\nassert.match(s, 'render(Foo)');\nconst re = /render\\(x\\)/;`;
  assert.equal(
    cls(inString).offender,
    true,
    "render( inside a string or regex literal must not count",
  );
  const blockThenCode = `${renderImported}\n/* note */ render(C);`;
  assert.equal(
    cls(blockThenCode).offender,
    false,
    "a real call after a closing block comment still counts",
  );
  const tmpl = `import { readFileSync } from 'node:fs';\nreadFileSync(\`\${root}/src/lib/Foo.svelte\`, 'utf8');`;
  assert.equal(
    cls(tmpl).offender,
    true,
    "a component path in a template literal must be flagged",
  );
  // Helpers resolve relative to the TEST file, so nested tests are seen (#5857 Codex P2).
  const readers = {
    readers: new Set(["engine-source.mjs"]),
    componentReaders: new Set(),
  };
  const nested = join(UNIT_DIR, "slow", "x.test.mjs");
  const nestedRead = `import { readFrontendSource } from '../engine-source.mjs';\nreadFrontendSource('src/lib/Foo.svelte');`;
  assert.equal(
    classify(nestedRead, readers, comps, nested).offender,
    true,
    "a nested test reading via ../helper must be flagged",
  );
  // Opposite: './engine-source.mjs' from slow/ is a DIFFERENT file, not the reader helper.
  const nestedOther = `import { readFrontendSource } from './engine-source.mjs';\nreadFrontendSource('src/lib/Foo.svelte');`;
  assert.equal(
    classify(nestedOther, readers, comps, nested).reads,
    false,
    "a same-named module elsewhere is not the helper",
  );
  const nestedRender = `import { loadSvelteSsrModule } from '../load-svelte-ssr.mjs';\n${reader}\nawait loadSvelteSsrModule(E);`;
  assert.equal(
    classify(nestedRender, none, comps, nested).offender,
    false,
    "a nested test rendering via ../helper is cleared",
  );
  // Namespace imports render too (#5857 Codex P2), and only from the right module.
  const nsRender = `import * as server from 'svelte/server';\n${reader}\nserver.render(C);`;
  assert.equal(
    cls(nsRender).offender,
    false,
    "a namespace svelte/server render clears the file",
  );
  const nsDyn = `const server = await import('svelte/server');\n${reader}\nserver.render(C);`;
  assert.equal(
    cls(nsDyn).offender,
    false,
    "a dynamic namespace render clears the file",
  );
  const nsWrong = `import * as other from './fixtures.mjs';\n${reader}\nother.render(C);`;
  assert.equal(
    cls(nsWrong).offender,
    true,
    "render on a namespace from another module does not count",
  );
  const nsFs = `import * as fs from 'node:fs';\nfs.readFileSync('src/lib/Foo.svelte', 'utf8');`;
  assert.equal(cls(nsFs).offender, true, "a namespace fs read is flagged");
  // A helper one level down is a reader too (#5857 Codex P2): readers are absolute paths.
  const nestedHelper = join(UNIT_DIR, "slow", "read-source.mjs");
  const nestedHelpers = {
    readers: new Set([nestedHelper]),
    componentReaders: new Set(),
  };
  const viaNested = `import { read } from './read-source.mjs';\nread('src/lib/Foo.svelte');`;
  assert.equal(
    classify(viaNested, nestedHelpers, comps, nested).offender,
    true,
    "a nested helper import must be flagged",
  );
  assert.equal(
    cls(viaNested, nestedHelpers).reads,
    false,
    "the same specifier from tests/unit is a different file",
  );
  // The helper scan itself recurses: a nested reader helper on disk is found.
  const tmp = mkdtempSync(join(tmpdir(), "ratchet-"));
  try {
    mkdirSync(join(tmp, "slow"));
    writeFileSync(
      join(tmp, "slow", "read-source.mjs"),
      `import { readFileSync } from 'node:fs';\nexport const read = () => readFileSync('src/lib/Foo.svelte', 'utf8');`,
    );
    writeFileSync(join(tmp, "plain.mjs"), `export const x = 1;`);
    const found = sourceReadingHelpers(tmp, comps);
    assert.deepEqual(
      [...found.readers],
      [join(tmp, "slow", "read-source.mjs")],
      "nested reader helper must be found",
    );
    assert.deepEqual(
      [...found.componentReaders],
      [join(tmp, "slow", "read-source.mjs")],
    );
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  const viaHelper = `import { BPM_LANE_BACKDROP } from './contrast.mjs';`;
  assert.equal(
    cls(viaHelper, {
      readers: new Set(["contrast.mjs"]),
      componentReaders: new Set(["contrast.mjs"]),
    }).offender,
    true,
    "a helper that itself reads a component must flag its importers",
  );
});

test("bundled Svelte client coverage must mount, not merely compile", () => {
  const reader = `import { readFileSync } from 'node:fs';
readFileSync('src/lib/Foo.svelte', 'utf8');`;
  const bundler = `import { bundleRecorderStatusRetryClient } from './mount-svelte-client.mjs';`;
  const helpers = { readers: new Set(), componentReaders: new Set() };
  const components = new Set(["Foo"]);
  const cls = (source) => classify(source, helpers, components);
  assert.equal(cls(`${reader}\n${bundler}`).offender, true);
  assert.equal(
    cls(`${reader}\n${bundler}\nconst client = await bundleRecorderStatusRetryClient();`).offender,
    true,
    "compiling without mounting is not rendered coverage",
  );
  assert.equal(
    cls(`${reader}\n${bundler}\nconst client = await bundleRecorderStatusRetryClient();\nclient.mount(C, { target });`).offender,
    false,
    "the bundled client runtime mounts the real component",
  );
  assert.equal(
    cls(`${reader}\nconst client = await unrelatedBundler();\nclient.mount(C, { target });`).offender,
    true,
    "a mount call without a known Svelte client bundler is insufficient",
  );
});

test("every entry has a file and a non-empty reason, no duplicates", () => {
  const seen = new Set();
  for (const e of allowlist) {
    assert.equal(typeof e.file, "string", `bad entry ${JSON.stringify(e)}`);
    assert.ok(
      typeof e.reason === "string" && e.reason.trim().length >= 10,
      `entry ${e.file} needs a specific reason`,
    );
    assert.ok(!seen.has(e.file), `duplicate allowlist entry ${e.file}`);
    seen.add(e.file);
  }
});

test("no NEW test reads .svelte source as text without rendering", () => {
  const allowed = new Set(allowlist.map((e) => e.file));
  const fresh = offenders.filter((f) => !allowed.has(f));
  assert.deepEqual(
    fresh,
    [],
    `These tests read a .svelte component as text and never render one:\n  ${fresh.join("\n  ")}\n` +
      `Render the component instead (example: tests/unit/${RENDERED_EXAMPLE}, which uses ` +
      `loadSvelteSsrModule with svelte/server's render), or add an entry to ` +
      `tests/unit/source-regex-test-allowlist.json with a SPECIFIC reason text is the right instrument.`,
  );
});

test("no STALE allowlist entry: the list only shrinks", () => {
  const found = new Set(offenders);
  const stale = allowlist
    .map((e) => e.file)
    .filter((f) => !found.has(f))
    .map(
      (f) =>
        `${f} (${existsSync(join(UNIT_DIR, f)) ? "no longer matches: converted?" : "file deleted"})`,
    );
  assert.deepEqual(
    stale,
    [],
    `Remove these entries from tests/unit/source-regex-test-allowlist.json:\n  ${stale.join("\n  ")}`,
  );
});
