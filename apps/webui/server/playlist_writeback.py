"""Compatibility shim: the playlist writeback service moved to apps.smartlists.

Moved in round 12 of the SMARTEST-CI counter so no domain package imports the
web UI: ``apps.smartlists.rb_writer`` and ``djay_writer`` reached in here for
the backup and conflict types, the last pair the ``webui-is-the-top-layer``
contract had to ignore. New code must import ``apps.smartlists.playlist_writeback``
directly rather than this shim.
"""

from apps.smartlists.playlist_writeback import *  # noqa: F403
from apps.smartlists.playlist_writeback import __all__  # noqa: F401
