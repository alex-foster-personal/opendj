// Which Chromium the real-CDP tests drive. Kept apart from the test so the search
// itself is tested against real directory layouts, not only the one this host has.
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const CFT_MAC = ["Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"];

/**
 * Where a Playwright `chromium-<revision>` directory keeps the browser binary for this
 * platform and architecture, newest layout first. Playwright 1.57 and later (the repo
 * pins 1.61.1) install Chrome for Testing builds in the `-64` and `mac-<arch>` layouts;
 * the older Chromium layouts stay for older installs. Only this host's layouts are
 * listed, so a cache shared across platforms never yields a binary this host cannot run
 * (Codex on #6830).
 */
export function playwrightChromiumLayouts(
  platform: NodeJS.Platform = process.platform,
  arch: string = process.arch,
): string[][] {
  if (platform === "win32") return [["chrome-win64", "chrome.exe"], ["chrome-win", "chrome.exe"]];
  if (platform === "darwin")
    return [
      [arch === "arm64" ? "chrome-mac-arm64" : "chrome-mac-x64", ...CFT_MAC],
      ["chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"],
    ];
  return [["chrome-linux64", "chrome"], ["chrome-linux", "chrome"]];
}

/**
 * Where Playwright keeps its browsers. PLAYWRIGHT_BROWSERS_PATH, when set, is the only
 * root, and one that does not exist throws rather than falling back to a cache the
 * caller did not name (Sol on #6830). Otherwise the per-user cache Playwright itself
 * derives: XDG_CACHE_HOME or ~/.cache on Linux, ~/Library/Caches on macOS, LOCALAPPDATA
 * or ~/AppData/Local on Windows (Codex on #6830), returned only when it exists. A relative
 * pinned root is resolved once here, so the binary found under it is absolute and launching
 * it never depends on the working directory (Codex on #6830).
 */
export function playwrightRoots(
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
  platform: NodeJS.Platform = process.platform,
): string[] {
  const pinned = env.PLAYWRIGHT_BROWSERS_PATH;
  if (pinned) {
    if (pinned === "0")
      throw new Error("PLAYWRIGHT_BROWSERS_PATH=0 keeps browsers inside node_modules; set CDP_TEST_BROWSER instead");
    if (!existsSync(pinned)) throw new Error(`PLAYWRIGHT_BROWSERS_PATH=${pinned} does not exist`);
    return [resolve(pinned)];
  }
  const cache =
    platform === "win32"
      ? env.LOCALAPPDATA || join(home, "AppData", "Local")
      : platform === "darwin"
        ? join(home, "Library", "Caches")
        : env.XDG_CACHE_HOME || join(home, ".cache");
  const root = join(cache, "ms-playwright");
  return existsSync(root) ? [root] : [];
}

/** The newest Playwright Chromium under `roots` for this host, by revision number, or null. */
export function playwrightChromium(
  roots: readonly string[],
  layouts: readonly (readonly string[])[] = playwrightChromiumLayouts(),
): string | null {
  for (const root of roots) {
    const revisions = readdirSync(root)
      .map((dir) => /^chromium-(\d+)$/.exec(dir))
      .filter((match): match is RegExpExecArray => match !== null)
      .sort((a, b) => Number(b[1]) - Number(a[1]));
    for (const { input: dir } of revisions) {
      for (const parts of layouts) {
        const candidate = join(root, dir, ...parts);
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  return null;
}

/**
 * CDP_TEST_BROWSER, else a Playwright Chromium, else Google Chrome on macOS, always as an
 * absolute path. None found throws, so the suite fails loudly rather than skipping.
 */
export function findBrowser(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): string {
  const explicit = env.CDP_TEST_BROWSER;
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`CDP_TEST_BROWSER=${explicit} does not exist`);
    return resolve(explicit);
  }
  const playwright = playwrightChromium(playwrightRoots(env));
  if (playwright) return playwright;
  if (platform === "darwin") {
    const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    if (existsSync(chrome)) return chrome;
  }
  throw new Error("no Chromium binary found: set CDP_TEST_BROWSER, or install Playwright's chromium");
}
