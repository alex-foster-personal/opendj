import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";
import { TEST_ADMIN_TOKEN, TEST_AIG_TOKEN, TEST_PLANS } from "./test/constants.ts";

export default defineConfig(async () => {
  const migrations = await readD1Migrations("./migrations");
  return {
    plugins: [
      cloudflareTest(({ inject }) => ({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            UPSTREAM_BASE_URL: inject("upstreamUrl"),
            CF_AIG_TOKEN: TEST_AIG_TOKEN,
            ADMIN_TOKEN: TEST_ADMIN_TOKEN,
            PLANS: TEST_PLANS,
            TEST_MIGRATIONS: migrations,
          },
        },
      })),
    ],
    test: { globalSetup: ["./test/upstream.global.ts"], setupFiles: ["./test/apply-migrations.ts"] },
  };
});
