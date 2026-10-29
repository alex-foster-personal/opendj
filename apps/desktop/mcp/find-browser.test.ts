// Run: pnpm --dir apps/desktop exec tsx --test mcp/find-browser.test.ts
// The browser search against real directory trees laid out the way each Playwright
// release installs Chromium on each platform. No test doubles.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative } from "node:path";
import { test } from "node:test";
import { findBrowser, playwrightChromium, playwrightChromiumLayouts, playwrightRoots } from "./find-browser";

/** A scratch browser cache holding one empty file at each relative path. */
function cache(files: string[][]): string {
  const root = mkdtempSync(join(tmpdir(), "find-browser-"));
  for (const parts of files) {
    const file = join(root, ...parts);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, "");
  }
  return root;
}

const CFT_MAC = ["Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"];

const HOSTS = [
  ["Linux, Playwright 1.61", "linux", "x64", ["chrome-linux64", "chrome"]],
  ["Windows, Playwright 1.61 (Codex on #6830)", "win32", "x64", ["chrome-win64", "chrome.exe"]],
  ["macOS arm64, Playwright 1.61 (Codex on #6830)", "darwin", "arm64", ["chrome-mac-arm64", ...CFT_MAC]],
  ["macOS x64, Playwright 1.61 (Codex on #6830)", "darwin", "x64", ["chrome-mac-x64", ...CFT_MAC]],
  ["Linux, older Playwright", "linux", "x64", ["chrome-linux", "chrome"]],
  ["Windows, older Playwright", "win32", "x64", ["chrome-win", "chrome.exe"]],
  ["macOS, older Playwright", "darwin", "arm64", ["chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"]],
] as const;

for (const [name, platform, arch, parts] of HOSTS) {
  test(`a Playwright Chromium is found in its ${name} layout`, () => {
    const root = cache([["chromium-1200", ...parts]]);
    try {
      const layouts = playwrightChromiumLayouts(platform, arch);
      assert.equal(playwrightChromium([root], layouts), join(root, "chromium-1200", ...parts));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}

test("a cache shared across platforms yields only this host's binary (Codex on #6830)", () => {
  // Every layout at once, as a cache restored from several runners would hold.
  const root = cache(HOSTS.map(([, , , parts]) => ["chromium-1200", ...parts]));
  try {
    for (const [name, platform, arch, parts] of HOSTS.slice(0, 4)) {
      assert.equal(
        playwrightChromium([root], playwrightChromiumLayouts(platform, arch)),
        join(root, "chromium-1200", ...parts),
        name,
      );
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("the newest revision wins by number, not by spelling", () => {
  const root = cache([
    ["chromium-999", "chrome-linux", "chrome"],
    ["chromium-1194", "chrome-linux", "chrome"],
  ]);
  try {
    assert.equal(
      playwrightChromium([root], playwrightChromiumLayouts("linux", "x64")),
      join(root, "chromium-1194", "chrome-linux", "chrome"),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a revision directory with no known layout, or no revision at all, finds nothing", () => {
  const root = cache([
    ["chromium-1200", "chrome-solaris", "chrome"],
    ["chromium_headless_shell-1200", "chrome-linux", "headless_shell"],
    ["chromium-1201-partial", "chrome-linux", "chrome"],
  ]);
  try {
    assert.equal(playwrightChromium([root], playwrightChromiumLayouts("linux", "x64")), null);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("CDP_TEST_BROWSER wins when it exists and is refused when it does not", () => {
  const root = cache([["browser"]]);
  try {
    assert.equal(findBrowser({ CDP_TEST_BROWSER: join(root, "browser") }), join(root, "browser"));
    assert.throws(() => findBrowser({ CDP_TEST_BROWSER: join(root, "missing") }), /does not exist/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a relative CDP_TEST_BROWSER or PLAYWRIGHT_BROWSERS_PATH comes back absolute (Codex on #6830)", () => {
  const root = cache([["browser"], ["pinned", "chromium-1200", "chrome-linux64", "chrome"]]);
  try {
    const rel = (...parts: string[]) => relative(process.cwd(), join(root, ...parts));
    assert.ok(!isAbsolute(rel("browser")), "control: the configured path really is relative");
    assert.equal(findBrowser({ CDP_TEST_BROWSER: rel("browser") }), join(root, "browser"));
    const [pinned] = playwrightRoots({ PLAYWRIGHT_BROWSERS_PATH: rel("pinned") }, join(root, "nobody"), "linux");
    assert.equal(pinned, join(root, "pinned"));
    assert.equal(
      playwrightChromium([pinned], [["chrome-linux64", "chrome"]]),
      join(root, "pinned", "chromium-1200", "chrome-linux64", "chrome"),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("the cache root follows Playwright's own variables, per platform (Codex on #6830)", () => {
  const root = cache([
    ["xdg", "ms-playwright", "x"],
    ["local", "ms-playwright", "x"],
    ["home", ".cache", "ms-playwright", "x"],
    ["home", "Library", "Caches", "ms-playwright", "x"],
    ["home", "AppData", "Local", "ms-playwright", "x"],
    ["pinned", "x"],
  ]);
  const at = (...parts: string[]) => join(root, ...parts);
  const home = at("home");
  try {
    assert.deepEqual(playwrightRoots({ XDG_CACHE_HOME: at("xdg") }, home, "linux"), [at("xdg", "ms-playwright")]);
    assert.deepEqual(playwrightRoots({}, home, "linux"), [at("home", ".cache", "ms-playwright")]);
    assert.deepEqual(playwrightRoots({ LOCALAPPDATA: at("local") }, home, "win32"), [at("local", "ms-playwright")]);
    assert.deepEqual(playwrightRoots({}, home, "win32"), [at("home", "AppData", "Local", "ms-playwright")]);
    assert.deepEqual(playwrightRoots({ XDG_CACHE_HOME: at("xdg") }, home, "darwin"), [
      at("home", "Library", "Caches", "ms-playwright"),
    ]);
    assert.deepEqual(playwrightRoots({ PLAYWRIGHT_BROWSERS_PATH: at("pinned") }, at("nobody"), "linux"), [at("pinned")]);
    // Set, the pinned root is the only one: the home cache that exists is not added.
    assert.deepEqual(playwrightRoots({ PLAYWRIGHT_BROWSERS_PATH: at("pinned") }, home, "linux"), [at("pinned")]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a PLAYWRIGHT_BROWSERS_PATH that does not exist throws, never falls back to the home cache (Sol on #6830)", () => {
  const root = cache([["home", ".cache", "ms-playwright", "chromium-1200", "chrome-linux64", "chrome"]]);
  try {
    const home = join(root, "home");
    assert.throws(
      () => playwrightRoots({ PLAYWRIGHT_BROWSERS_PATH: join(root, "gone") }, home, "linux"),
      /PLAYWRIGHT_BROWSERS_PATH=.*gone does not exist/,
    );
    assert.throws(() => playwrightRoots({ PLAYWRIGHT_BROWSERS_PATH: "0" }, home, "linux"), /CDP_TEST_BROWSER/);
    // Control: unset, the same home cache is found.
    assert.deepEqual(playwrightRoots({}, home, "linux"), [join(home, ".cache", "ms-playwright")]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
