#!/usr/bin/env bash
# GOOD FIXTURE for rule `zsh-modifier-path`: braces end the parameter name
# before the colon, so no modifier is parsed and both shells agree.
set -euo pipefail

branch_has_file() {
    SHA="$1"
    git cat-file -e "${SHA}:apps/webui/server/app.py"
}

count_file_lines() {
    S="$1"
    git show "${S}:apps/webui/server/app.py" | wc -l
}

# A colon followed by a digit is a port. No modifier is a digit.
ping_engine() {
    curl -fsS "http://$HOST:8683/api/v1/health"
}

# Single quotes stop expansion entirely, so the construct is inert there.
print_template() {
    printf '%s\n' '$SHA:apps/webui/server/app.py'
}
