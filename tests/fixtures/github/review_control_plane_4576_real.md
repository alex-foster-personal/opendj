# review_control_plane_4576_real.json

Captured Thu 1 Oct 2026 with `gh api --paginate` from merged PR #4576 at its head
`141a996f7532014bb93830b1c0c9d055fe2fb25c`, the incident REVIEW-13 exists for: it edits
`.github/workflows/periodic-checks.yml` (control plane), its commits carry `-Claude`, and it
merged with only Sol's submitted review at head.

Projection, nothing invented: each record keeps only the fields review_coverage and
review_control_plane read (`filename`/`previous_filename`; commit `sha`, `parents`, `message`;
review and comment `user.login`, `commit_id`/`original_commit_id`, `state`, timestamps, `body`).
Every record GitHub returned is present, in GitHub's order.
