/**
 * Trunk Flaky Tests -> GitHub Issues (maintainer/music-dj-tools).
 * Event: v2.test_case.status_changed. Files one issue when a test turns FLAKY or BROKEN;
 * every other transition (including back to HEALTHY) is cancelled, not sent.
 *
 * Fail fast: every field this reads is required by Trunk's published schema, and each is
 * type-checked before use. A missing or mistyped one cancels the delivery with an explicit
 * `payload.error` and files nothing, never an issue built from invented values. A value the
 * schema allows to be empty (file_path "", codeowners []; Trunk's own published example has
 * both) renders literally as "(empty)".
 *
 * This file is the canonical copy of the Svix transformation on the Trunk webhook endpoint
 * (Trunk > Settings > Webhooks > endpoint > Advanced > Transformation). The endpoint URL
 * (POST /repos/<owner>/<repo>/issues) and its Authorization header live in the portal, never
 * here. Edit this file first, then paste it into the portal. Tested by
 * tests/scripts/test_trunk_flaky_webhook_transform.py.
 */
const LABELS = ["ci:flaky-test", "area:test-infra"];
const FILE_ON = ["FLAKY", "BROKEN"];
const EMPTY = "(empty)";
const PAYLOAD_TYPES = { new_status: "string", previous_status: "string", timestamp: "string", test_case: "object" };
const TEST_CASE_TYPES = { name: "string", file_path: "string", quarantined: "boolean", codeowners: "string[]", html_url: "string" };

function typeError(value, expected) {
  const ok =
    expected === "string[]"
      ? Array.isArray(value) && value.every((item) => typeof item === "string")
      : expected === "object"
        ? value !== null && typeof value === "object" && !Array.isArray(value)
        : typeof value === expected;
  if (ok) return null;
  return value === undefined ? "missing" : `expected ${expected}, got ${JSON.stringify(value)}`;
}

function schemaErrors(p) {
  const errors = [];
  for (const [key, expected] of Object.entries(PAYLOAD_TYPES)) {
    const problem = typeError(p[key], expected);
    if (problem) errors.push(`${key}: ${problem}`);
  }
  if (errors.some((e) => e.startsWith("test_case:"))) return errors;
  for (const [key, expected] of Object.entries(TEST_CASE_TYPES)) {
    const problem = typeError(p.test_case[key], expected);
    if (problem) errors.push(`test_case.${key}: ${problem}`);
  }
  return errors;
}

function handler(webhook) {
  const p = webhook.payload;
  const errors = schemaErrors(p);
  if (errors.length) {
    webhook.cancel = true;
    webhook.payload = { error: `trunk flaky webhook rejected a malformed event: ${errors.join("; ")}` };
    return webhook;
  }
  if (!FILE_ON.includes(p.new_status)) {
    webhook.cancel = true;
    return webhook;
  }
  const t = p.test_case;
  webhook.payload = {
    title: `[${p.new_status}] ${t.name}`.slice(0, 250),
    body: [
      `Trunk marked this test **${p.new_status}** (was ${p.previous_status}) at ${p.timestamp}.`,
      "",
      `- Test: \`${t.name}\``,
      `- File: ${t.file_path === "" ? EMPTY : `\`${t.file_path}\``}`,
      `- Quarantined: ${t.quarantined ? "yes" : "no"}`,
      `- Code owners: ${t.codeowners.length ? t.codeowners.join(", ") : EMPTY}`,
      `- Trunk detail page: ${t.html_url}`,
      "",
      "Filed automatically by the Trunk Flaky Tests webhook (ADR-NEW-trunk-merge-queue-replaces-mergify).",
    ].join("\n"),
    labels: LABELS,
  };
  return webhook;
}
