"""Explorer source backend: the collection as a folder tree of its stored file paths (issue #9747, PR 1 of 2).

``GET /api/v1/library/folders`` is the tree with recursive and direct track counts, built from one read of every
track's stored path; it never walks the disk. ``GET /api/v1/library/folders/tracks?path=`` lists the tracks directly
in one folder, or under it with ``recursive=true``, in file-name order. The browser UI is a follow-up.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apps.shared.folder_tree import FolderNode, build_folder_forest

from ..backend import StateBackend
from ..deps import get_read_state

router = APIRouter(prefix="/library/folders", tags=["library"])
MOUNT_UNDER_API_PREFIX = True


class FolderNodeOut(BaseModel):
    name: str
    path: str
    track_count: int
    direct_track_count: int
    children: list[FolderNodeOut]


class FolderTreeOut(BaseModel):
    roots: list[FolderNodeOut]
    track_count: int
    unlocated_count: int


class FolderTrackOut(BaseModel):
    stable_id: str
    file_path: str


class FolderTracksOut(BaseModel):
    path: str
    recursive: bool
    track_count: int
    items: list[FolderTrackOut]


def _node_out(node: FolderNode) -> FolderNodeOut:
    return FolderNodeOut(
        name=node.name,
        path=node.path,
        track_count=node.track_count(),
        direct_track_count=len(node.tracks),
        children=[_node_out(child) for child in node.sorted_children()],
    )


@router.get("", response_model=FolderTreeOut, operation_id="get_library_folders")
def get_library_folders(backend: StateBackend = Depends(get_read_state)) -> FolderTreeOut:  # noqa: B008  # FastAPI DI
    """The folder tree of every track's stored path, with counts."""
    forest = build_folder_forest(backend.list_file_paths())
    roots = [_node_out(root) for root in forest.sorted_roots()]
    return FolderTreeOut(
        roots=roots,
        track_count=sum(root.track_count for root in roots),
        unlocated_count=len(forest.unlocated_ids),
    )


@router.get("/tracks", response_model=FolderTracksOut, operation_id="get_library_folder_tracks")
def get_library_folder_tracks(
    path: str = Query(min_length=1, description="A folder path exactly as GET /library/folders returns it"),
    recursive: bool = Query(False, description="Include tracks in every folder below this one"),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> FolderTracksOut:
    """The tracks in one folder (or under it), in file-name order; 404 when no track lives there."""
    node = build_folder_forest(backend.list_file_paths()).find(path)
    if node is None:
        raise HTTPException(status_code=404, detail={"error": "folder_not_found", "path": path})
    items = [FolderTrackOut(stable_id=sid, file_path=fp) for sid, fp in node.tracks_in_order(recursive=recursive)]
    return FolderTracksOut(path=node.path, recursive=recursive, track_count=len(items), items=items)


__all__ = ["router"]
