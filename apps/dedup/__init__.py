"""apps.dedup -- Phase 7 dedup pipeline.

Subcommands:

* ``python -m apps.dedup.scan``          -- fingerprint + cache library.
* ``python -m apps.dedup.find_clusters`` -- cluster + canonical pick.
* ``python -m apps.dedup.apply``         -- dry-run or cautious live DB
                                            playlist-link rewrite.
"""
