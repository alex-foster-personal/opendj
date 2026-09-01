#!/usr/bin/env bash
# BAD FIXTURE for scripts/shell_construct_lint.py rule `zsh-modifier-path`.
# Never sourced, never run.
#
# Both cases below are the live instances. In zsh the character after the colon
# is read as a history modifier, so the path is mangled or eaten:
#
#   "$SHA:apps/..."  ->  :a is the absolute-path modifier. The `a` is eaten and
#                        the cwd is prefixed, so cat-file -e degenerates into
#                        "does this commit exist", which is true, exit 0.
#   "$S:apps/..."    ->  same mangling under git show. Measured 0 lines on one
#                        run and 1 on the next, which is why a value-based
#                        check for a suspicious zero would have passed.
set -euo pipefail

branch_has_file() {
    SHA="$1"
    git cat-file -e "$SHA:apps/webui/server/app.py"
}

count_file_lines() {
    S="$1"
    git show "$S:apps/webui/server/app.py" | wc -l
}
