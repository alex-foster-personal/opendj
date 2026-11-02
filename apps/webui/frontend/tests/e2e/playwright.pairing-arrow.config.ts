/**
 * Pairing PAIR-arrow browser spec: its own tier, its own fixture (Codex P1, #6062).
 *
 * The arrow draws only when a master-deck track has a paired partner that is in
 * the table but scrolled out of view. The root suite's 2-track library cannot
 * produce that, so this config boots the real engine against a 26-track
 * throwaway library (`--seed-pairing-arrow`) and the real /performance UI.
 *
 * Run with: `just test-pairing-arrow`
 */
import { defineConfig, devices } from "@playwright/test";
import { accessSync, constants, mkdirSync } from "node:fs";
import { delimiter, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  PAIRING_ARROW_API_PORT,
  PAIRING_ARROW_FRONTEND_PORT,
} from "./vite.pairing-arrow.config";
import { guardedWebServerCommand } from "./support/guarded-web-server";

const FRONTEND_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(
  new URL("../../../../..", import.meta.url),
);
const FRONTEND_ORIGIN = `http://127.0.0.1:${PAIRING_ARROW_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${PAIRING_ARROW_API_PORT}`;

export const FIXTURE_DATA_DIR = join(
  FRONTEND_ROOT,
  "tests",
  "e2e",
  "fixtures",
  "pairing-arrow-data",
);
export const FIXTURE_MANIFEST_PATH = join(
  FIXTURE_DATA_DIR,
  "fixture-manifest.json",
);

/** HOME for the engine under test, so it cannot see a real rekordbox library. */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, "sandbox-home");
mkdirSync(SANDBOX_HOME, { recursive: true });

function shellArgument(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

/** Absolute path of a tool on PATH. The server commands run under /bin/sh, so a
 * bare name would depend on that shell's lookup; the repository contract is to
 * launch programs by resolved path. On Windows the servers are skipped, so the
 * bare name is returned unchanged there. */
function resolveTool(name: string): string {
  if (process.platform === "win32") return name;
  for (const dir of (process.env.PATH ?? "").split(delimiter)) {
    if (dir === "") continue;
    const candidate = join(dir, name);
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Not in this directory; keep looking.
    }
  }
  throw new Error(`${name} is not on PATH; the pairing-arrow servers cannot start without it`);
}

const UV = resolveTool("uv");
const PNPM = resolveTool("pnpm");

const FIXTURE_COMMAND = [
  UV,
  "run",
  "--no-sync",
  "python",
  "-m",
  "apps.webui.frontend.tests.e2e.support.deckload_fixture",
  "--data-dir",
  FIXTURE_DATA_DIR,
  "--seed-pairing-arrow",
  "--manifest",
  FIXTURE_MANIFEST_PATH,
]
  .map(shellArgument)
  .join(" ");
const ENGINE_COMMAND = [
  UV,
  "run",
  "--no-sync",
  "python",
  "-m",
  "apps.engine_core",
  "serve",
  "--data-dir",
  FIXTURE_DATA_DIR,
  "--host",
  "127.0.0.1",
  "--port",
  String(PAIRING_ARROW_API_PORT),
]
  .map(shellArgument)
  .join(" ");
const SERVER_COMMAND = `${FIXTURE_COMMAND} && ${ENGINE_COMMAND}`;
/** The guarded servers are POSIX-only; see the webServer note below. */
export const POSIX_SERVERS = process.platform !== "win32";

export default defineConfig({
  // Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
  captureGitInfo: { commit: false, diff: false },
  testDir: ".",
  testMatch: "pairing-arrow.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  outputDir: join(FRONTEND_ROOT, "test-results", "pairing-arrow"),
  // The server guard (guarded-web-server.ts) is POSIX-only: it runs
  // .venv/bin/python under /bin/sh. On Windows no servers start and the spec
  // skips itself with that reason, so this config still loads everywhere.
  webServer: !POSIX_SERVERS
    ? []
    : [
        {
          command: guardedWebServerCommand(
            "pairing-arrow-engine",
            SERVER_COMMAND,
          ),
          cwd: REPOSITORY_ROOT,
          url: `${API_ORIGIN}/api/v1/health`,
          reuseExistingServer: false,
          timeout: 180_000,
          env: {
            ...process.env,
            MDT_DATA_DIR: FIXTURE_DATA_DIR,
            MDT_LIBRARY_MODE: "local",
            WEB_CONCURRENCY: "",
            HOME: SANDBOX_HOME,
          },
        },
        {
          command: guardedWebServerCommand(
            "pairing-arrow-vite",
            `${shellArgument(PNPM)} exec vite --config tests/e2e/vite.pairing-arrow.config.ts`,
          ),
          cwd: FRONTEND_ROOT,
          url: `${FRONTEND_ORIGIN}/performance`,
          reuseExistingServer: false,
          timeout: 90_000,
        },
      ],
  use: {
    baseURL: FRONTEND_ORIGIN,
    viewport: { width: 1600, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "pairing-arrow-chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
