/**
 * Connect Spotify from a plain browser tab opens its sign-in window (SPOT-6555).
 *
 * The bug this pins: Connect awaited
 * POST /account/connect before calling window.open, so a reply slower than
 * Chromium's transient user activation (5 s) left the open without a user
 * gesture and the browser blocked it, while the panel said "finish signing in"
 * and polled for five minutes.
 *
 * Runs the production path end to end: the root suite's
 * real engine answers /api/v1/spotify/account/*, so the panel, the FastAPI
 * route, ConnectFlow's redirect listener and its authorize URL are all real.
 * The only interventions are network ones: the real connect reply is held
 * past the activation window before it is passed through unchanged, and the
 * sign-in window's request to accounts.spotify.com is recorded and aborted so
 * the test never leaves the machine.
 *
 * Prerequisites, both real: the spotify extra installed in the engine's venv
 * (e2e.yml installs it), and a saved client id in the engine's HOME, written
 * the way a past sign-in leaves it. With no token the engine reports
 * not_signed_in, which is what puts Connect on screen.
 *
 * [if] the sign-in window is not opened, or never asks Spotify to authorize
 *   this client with PKCE [then] stop.
 * control [if] Chromium's pop-up blocker refuses the window (a click with no
 *   user activation) [then] the panel says so and never asks the engine to
 *   start a sign-in (no listener left waiting).
 */
// requirement: SPOT-6555
import { expect, test, type Page, type Request } from "@playwright/test";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// The root config starts the engine with HOME here (playwright.config.ts).
const SANDBOX_HOME = fileURLToPath(
  new URL("./fixtures/root-playwright-data/sandbox-home", import.meta.url),
);
// apps/spotify/account.py CLIENT_ID_PATH, under that HOME.
const CLIENT_ID_FILE = join(
  SANDBOX_HOME,
  ".music-dj-tools",
  "spotify-client.json",
);
const CLIENT_ID = "e2e0000000000000000000000000000c";
// What Python's json.dumps writes there (note the space after the colon).
const SAVED_BYTES = `{"client_id": "${CLIENT_ID}"}\n`;

// Chromium's transient user activation lasts 5 s; hold the reply past it.
const CONNECT_DELAY_MS = 6_000;
const CONNECT_PATH = "/api/v1/spotify/account/connect";

// Playwright launches Chromium with --disable-popup-blocking; drop it so the
// browser's own blocker judges every window.open, as it does for users.
// Headless runs default to chromium-headless-shell, which has no pop-up
// blocker at all (the control below failed on it in CI at 4088885), so pin
// the full Chromium build, whose new headless mode blocks as a user's does.
test.use({
  channel: "chromium",
  launchOptions: async ({ launchOptions }, use) => {
    await use({
      ...launchOptions,
      ignoreDefaultArgs: ["--disable-popup-blocking"],
    });
  },
});

function saveClientId(): void {
  mkdirSync(join(SANDBOX_HOME, ".music-dj-tools"), { recursive: true });
  // The exact bytes apps.spotify.account.save_client_id writes.
  // Explicit UTF-8; SAVED_BYTES already ends in an LF.
  writeFileSync(CLIENT_ID_FILE, SAVED_BYTES, { encoding: "utf8" });
}

test.afterAll(() => {
  rmSync(CLIENT_ID_FILE, { force: true });
});

test("a fresh install pastes its Client ID in the panel, then Connect appears", async ({
  page,
}) => {
  // With no env id and no saved
  // id, Connect was hidden and nothing in the app could ever save one.
  // playwright.config.ts starts the engine with SPOTIFY_CLIENT_ID empty, so
  // this runs with or without a credential in the caller's environment.
  rmSync(CLIENT_ID_FILE, { force: true });
  await page.goto("/performance?source=spotify");
  await expect(
    page.getByText("Spotify needs a Client ID once before you can sign in."),
  ).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    page.getByRole("button", { name: "Connect Spotify" }),
  ).toHaveCount(0);
  const input = page.getByRole("textbox", { name: "Spotify Client ID" });
  const save = page.getByRole("button", { name: "Save Spotify Client ID" });
  await input.fill("not-a-client-id");
  await expect(save).toBeDisabled();
  expect(existsSync(CLIENT_ID_FILE)).toBe(false);

  await input.fill(CLIENT_ID.toUpperCase());
  await save.click();
  // The engine saved it (lowercased, the exact bytes save_client_id writes)
  // and now reports not_signed_in, which is what puts Connect on screen.
  await expect(
    page.getByRole("button", { name: "Connect Spotify" }),
  ).toBeVisible({ timeout: 30_000 });
  expect(readFileSync(CLIENT_ID_FILE, "utf-8")).toBe(SAVED_BYTES);
  await expect(input).toHaveCount(0);
});

async function openSpotifySource(page: Page): Promise<void> {
  saveClientId();
  await page.goto("/performance?source=spotify");
  await expect(
    page.getByRole("button", { name: "Connect Spotify" }),
  ).toBeVisible({
    timeout: 30_000,
  });
}

function isConnectPost(request: Request): boolean {
  return (
    request.method() === "POST" &&
    new URL(request.url()).pathname === CONNECT_PATH
  );
}

test("a slow connect reply still opens the sign-in window at the authorize URL", async ({
  page,
  context,
}) => {
  test.setTimeout(60_000);
  const authorizeRequests: string[] = [];
  await context.route("https://accounts.spotify.com/**", async (route) => {
    authorizeRequests.push(route.request().url());
    await route.abort();
  });
  await page.route(`**${CONNECT_PATH}`, async (route) => {
    if (isConnectPost(route.request())) {
      await new Promise((resolve) => setTimeout(resolve, CONNECT_DELAY_MS));
    }
    await route.continue();
  });
  await openSpotifySource(page);
  // The engine's real not-signed-in answer, as the panel words it.
  await expect(
    page.getByText("Sign in to list all your Spotify playlists."),
  ).toBeVisible();

  const popupPromise = page.waitForEvent("popup", {
    timeout: CONNECT_DELAY_MS + 3_000,
  });
  await page.getByRole("button", { name: "Connect Spotify" }).click();
  const popup = await popupPromise;
  // The sign-in window is cut off from the panel that opened it.
  expect(await popup.evaluate(() => window.opener)).toBeNull();
  await expect
    .poll(() => authorizeRequests.length, {
      timeout: CONNECT_DELAY_MS + 10_000,
    })
    .toBe(1);

  const authorize = new URL(authorizeRequests[0]);
  expect(`${authorize.origin}${authorize.pathname}`).toBe(
    "https://accounts.spotify.com/authorize",
  );
  // The engine runs with SPOTIFY_CLIENT_ID empty (playwright.config.ts), so
  // the id is the one saved in its HOME.
  expect(authorize.searchParams.get("client_id")).toBe(CLIENT_ID);
  expect(authorize.searchParams.get("response_type")).toBe("code");
  // The root config asks for an ephemeral listener (playwright.config.ts),
  // so concurrent suites on one host never share 8888.
  const redirect = authorize.searchParams.get("redirect_uri") ?? "";
  const port = /^http:\/\/127\.0\.0\.1:(\d+)\/callback$/.exec(redirect)?.[1];
  expect(port, redirect).toBeDefined();
  expect(Number(port)).not.toBe(8888);
  expect(authorize.searchParams.get("code_challenge_method")).toBe("S256");
  expect(authorize.searchParams.get("code_challenge")).toBeTruthy();
  expect(authorize.searchParams.get("state")).toBeTruthy();
  await expect(
    page.getByText("Finish signing in in your browser."),
  ).toBeVisible();
});

test("control: a blocked window is reported and starts no sign-in", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const connectPosts: string[] = [];
  page.on("request", (request) => {
    if (isConnectPost(request)) connectPosts.push(request.url());
  });
  await openSpotifySource(page);
  // Playwright runs evaluate with a user gesture, so click once that
  // activation has expired: Chromium's blocker then refuses the window
  // exactly as it refuses one opened after a slow reply.
  await page.evaluate(
    (delay) =>
      new Promise<void>((resolve) =>
        setTimeout(() => {
          const button = [...document.querySelectorAll("button")].find(
            (b) => b.textContent === "Connect Spotify",
          );
          button?.click();
          resolve();
        }, delay),
      ),
    CONNECT_DELAY_MS,
  );
  await expect(
    page.getByText(/blocked the Spotify sign-in window/),
  ).toBeVisible();
  expect(connectPosts).toEqual([]);
});
