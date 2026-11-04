Control for `scripts/doc_path_leak.py` (row `doc-path-leak` in `scripts/security/scan_secrets.sh`).

`@HOME@` and `@MACHOME@` are replaced at run time with a Linux and a macOS home path for a
login generated per run, so no tracked file names a home directory. After substitution,
`control.diff` against `head/` must yield exactly three findings: `(private path):3` (worktree
path), `(private path):9` (inside a `bash` fence, which is not a transcript) and
`(private path):2`. Every other planted line is an exemption that must hold.
