"""Real contract packages for `scripts.arch_contract_schema`'s tests.

Each ``<case>/contract`` is imported the way ``apps.<pkg>.contract`` is, through
`declaration_problems(<case>, container="tests.quality.contract_fixtures")`. Most break the
declaration rule on purpose; ``clean`` holds every construct the rule must let through.
"""
