/**
 * Trunk Flaky Tests -> GitHub Issues (maintainer/music-dj-tools).
 * Event: v2.test_case.status_changed. Files one issue when a test turns FLAKY or BROKEN;
 * every other transition (including back to HEALTHY) is cancelled, not sent.
 *
 * This file is the canonical copy of the Svix transformation on the Trunk webhook endpoint
 * (Trunk > Settings > Webhooks > endpoint > Advanced > Transformation). The endpoint URL
 * (POST /repos/<owner>/<repo>/issues) and its Authorization header live in the portal, never
 * here. Edit this file first, then paste it into the portal. Tested by
 * tests/scripts/test_trunk_flaky_webhook_transform.py.
 */
const LABELS = ["ci:flaky-test", "area:test-infra"];
const FILE_ON = ["FLAKY", "BROKEN"];

function handler(webhook) {
  const p = webhook.payload;
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
      `- File: \`${t.file_path || "unknown"}\``,
      `- Quarantined: ${t.quarantined ? "yes" : "no"}`,
      `- Code owners: ${(t.codeowners && t.codeowners.length ? t.codeowners : ["none"]).join(", ")}`,
      `- Trunk detail page: ${t.html_url}`,
      "",
      "Filed automatically by the Trunk Flaky Tests webhook (ADR-NEW-trunk-merge-queue-replaces-mergify).",
    ].join("\n"),
    labels: LABELS,
  };
  return webhook;
}
