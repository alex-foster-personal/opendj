"""Codex, PR #6182: a directory with no __init__.py still imports, as a namespace package."""


class Unread:  # neither frozen nor a model, and only a namespace package holds it
    pass
