"""Screenshot attachments on comment pins (issue #1333, part 2 of #928).

[if] a valid image is uploaded to an existing pin [then] it is stored under
    data-dir/feedback/attachments/ and the pin's attachment record (visible
    over GET /comments, i.e. survives a reload) points at a fetchable url
[if] the upload exceeds the size policy [then ⛔️] 413 with an explicit
    message naming the limit, nothing written to disk or comments.json
[if] the declared content type is not one of PNG/JPEG/GIF/WebP [then ⛔️] 415,
    nothing written
[if] the bytes do not actually decode as the declared image type [then ⛔️]
    422, nothing written (a spoofed Content-Type header must not pass)
[if] the bytes decode as a real image but NOT the one the Content-Type header
    claims (e.g. a GIF posted as image/png) [then ⛔️] 422
    ATTACHMENT_TYPE_MISMATCH, nothing written (PR #1425 P2)
[if] the bytes decode to an image past Pillow's decompression-bomb limit
    [then ⛔️] 413 ATTACHMENT_TOO_LARGE with a clear reason, never a bare 500
    (PR #1425 P2)
[if] the target pin does not exist [then ⛔️] 404, nothing written
[if] a pin already carries an attachment and gets a second one [then] the old
    file is deleted and only the new attachment is referenced
[if] an attachment id is fetched that was never minted [then ⛔️] 404, and a
    malformed id never reaches glob() as a path pattern
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app)


def _create(client: TestClient, text: str = "the header icon is misaligned") -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "anchor": ".bank",
            "page": "/performance",
            "text": text,
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _comments(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "feedback" / "comments.json").read_text())["comments"]


def _attachment_files(tmp_path: Path) -> list[Path]:
    att_dir = tmp_path / "feedback" / "attachments"
    return sorted(att_dir.glob("*")) if att_dir.is_dir() else []


def _png_bytes(color: tuple[int, int, int] = (255, 0, 0), size: tuple[int, int] = (4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


# ----- upload: the happy path ----------------------------------------------
def test_upload_stores_the_file_and_returns_a_fetchable_url(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    png = _png_bytes()

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("screenshot.png", png, "image/png")},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    attachment = out["attachment"]
    assert attachment["content_type"] == "image/png"
    assert attachment["size_bytes"] == len(png)
    assert attachment["url"] == f"/api/v1/feedback/attachments/{attachment['id']}"

    files = _attachment_files(tmp_path)
    assert len(files) == 1
    assert files[0].name == f"{attachment['id']}.png"
    assert files[0].read_bytes() == png

    # "renders after reload": re-fetching the pin list must show it too, not
    # just the direct response to the upload call.
    reloaded = client.get("/api/v1/feedback/comments").json()["comments"]
    assert reloaded[0]["attachment"]["id"] == attachment["id"]


def test_agent_can_read_and_fetch_the_attachment_over_the_api(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    png = _png_bytes(color=(0, 255, 0))
    upload = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", png, "image/png")},
    ).json()

    fetched = client.get(upload["attachment"]["url"])
    assert fetched.status_code == 200
    assert fetched.content == png
    assert fetched.headers["content-type"] == "image/png"


# ----- size policy -----------------------------------------------------------
def test_oversized_upload_is_refused_with_an_explicit_message_and_writes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    from apps.webui.server.routes import feedback_attachments

    pin = _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    oversized = b"\xff" * (feedback_attachments.MAX_ATTACHMENT_BYTES + 1)

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("huge.png", oversized, "image/png")},
    )
    assert r.status_code == 413, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "ATTACHMENT_TOO_LARGE"
    assert str(feedback_attachments.MAX_ATTACHMENT_BYTES) in detail["message"], (
        "the refusal must name the limit, never a bare 'too big'"
    )
    assert _attachment_files(tmp_path) == [], "a refused upload must leave no file behind"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before


def test_upload_boundary_exact_limit_succeeds_one_byte_over_is_refused(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutate the guard in both directions (house verification rule): the
    same policy check must let the exact limit through and refuse one byte
    more, not just refuse something obviously huge."""
    from apps.webui.server.routes import feedback_attachments

    pin = _create(client)
    png = _png_bytes()

    monkeypatch.setattr(feedback_attachments, "MAX_ATTACHMENT_BYTES", len(png))
    ok = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("exact.png", png, "image/png")},
    )
    assert ok.status_code == 201, ok.text

    monkeypatch.setattr(feedback_attachments, "MAX_ATTACHMENT_BYTES", len(png) - 1)
    over = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("over.png", png, "image/png")},
    )
    assert over.status_code == 413
    assert over.json()["detail"]["code"] == "ATTACHMENT_TOO_LARGE"


# ----- type policy -----------------------------------------------------------
def test_unsupported_content_type_is_refused_and_writes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )
    assert r.status_code == 415, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "ATTACHMENT_TYPE_UNSUPPORTED"
    assert "text/plain" in detail["message"]
    assert _attachment_files(tmp_path) == []
    assert _comments(tmp_path)[0].get("attachment") is None


def test_bytes_that_do_not_decode_as_the_declared_type_are_refused(
    client: TestClient, tmp_path: Path
) -> None:
    # A spoofed Content-Type header must not get past declared-type checking
    # alone: the bytes below are not a PNG at all.
    pin = _create(client)
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("fake.png", b"not actually a png file at all", "image/png")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_NOT_AN_IMAGE"
    assert _attachment_files(tmp_path) == []


# ----- declared type vs decoded format (PR #1425 P2) -------------------------
def test_declared_content_type_mismatched_with_the_decoded_format_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    """`Image.open(...).verify()` alone only proves the bytes decode as SOME
    image, not the CLAIMED one: real GIF bytes posted as `image/png` must not
    be stored as `<id>.png`, recorded as `image/png`, and served back that
    way - a wrong answer for any agent trusting `attachment.content_type`."""
    pin = _create(client)
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="GIF")
    gif_bytes_declared_as_png = buf.getvalue()

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", gif_bytes_declared_as_png, "image/png")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_TYPE_MISMATCH"
    assert _attachment_files(tmp_path) == []
    assert _comments(tmp_path)[0].get("attachment") is None


def test_declared_content_type_matching_the_decoded_format_still_succeeds(
    client: TestClient, tmp_path: Path
) -> None:
    """Mutate the guard in both directions (house verification rule): the
    same check that refuses a mismatch above must let a genuinely matching
    upload of every supported type through."""
    for suffix, fmt, content_type in [
        ("png", "PNG", "image/png"),
        ("jpg", "JPEG", "image/jpeg"),
        ("gif", "GIF", "image/gif"),
        ("webp", "WEBP", "image/webp"),
    ]:
        pin = _create(client, text=f"matching {fmt}")
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), color=(9, 9, 9)).save(buf, format=fmt)
        r = client.post(
            f"/api/v1/feedback/comments/{pin['id']}/attachment",
            files={"file": (f"shot.{suffix}", buf.getvalue(), content_type)},
        )
        assert r.status_code == 201, (fmt, r.text)
        assert r.json()["attachment"]["content_type"] == content_type


# ----- decompression bomb (PR #1425 P2) --------------------------------------
def test_a_decompression_bomb_image_is_refused_with_413_not_a_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIL.Image.DecompressionBombError subclasses Exception directly (not
    OSError/SyntaxError/ValueError), so without an explicit catch it escapes
    the except tuple and turns into a bare 500 - breaking the module's own
    'a refusal is always an explicit 4xx' contract. A small file declaring a
    huge canvas must be refused, not crash the handler."""
    from apps.webui.server.routes import feedback_attachments

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    pin = _create(client)
    buf = io.BytesIO()
    # 4000x4000 = 16,000,000 pixels: well past 2x the lowered limit, where
    # Pillow raises DecompressionBombError outright (not just a warning) -
    # but the PNG file itself stays tiny (solid color compresses hard).
    Image.new("RGB", (4000, 4000), color=(4, 5, 6)).save(buf, format="PNG")
    bomb = buf.getvalue()
    assert len(bomb) < feedback_attachments.MAX_ATTACHMENT_BYTES, (
        "the file must pass the byte-size check so the pixel-count check is what fires"
    )

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("bomb.png", bomb, "image/png")},
    )
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_TOO_LARGE"
    assert _attachment_files(tmp_path) == []
    assert _comments(tmp_path)[0].get("attachment") is None


def test_a_near_limit_image_that_only_warns_is_also_refused_not_silently_decoded(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between 1x and 2x Image.MAX_IMAGE_PIXELS, Pillow raises
    DecompressionBombWarning rather than -Error. The warning must be
    promoted to a refusal too, or a near-limit bomb silently decodes
    instead of failing the same explicit-4xx contract as the error case."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    pin = _create(client)
    buf = io.BytesIO()
    # 1100x1100 = 1,210,000 pixels: ~1.21x the lowered limit, inside the
    # warn-not-error band (below the 2x point where it becomes an error).
    Image.new("RGB", (1100, 1100), color=(7, 8, 9)).save(buf, format="PNG")
    near_limit_bomb = buf.getvalue()

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("near.png", near_limit_bomb, "image/png")},
    )
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_TOO_LARGE"
    assert _attachment_files(tmp_path) == []


def test_empty_upload_is_refused(client: TestClient, tmp_path: Path) -> None:
    pin = _create(client)
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_EMPTY"
    assert _attachment_files(tmp_path) == []


# ----- unknown comment -------------------------------------------------------
def test_upload_to_an_unknown_comment_is_404_and_writes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    r = client.post(
        "/api/v1/feedback/comments/nope/attachment",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"
    assert _attachment_files(tmp_path) == [], (
        "a valid image aimed at a nonexistent pin must not leave an orphan file"
    )


# ----- replacing an existing attachment --------------------------------------
def test_a_second_upload_replaces_the_first_and_deletes_its_file(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    first = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("one.png", _png_bytes(color=(1, 2, 3)), "image/png")},
    ).json()["attachment"]
    first_path = tmp_path / "feedback" / "attachments" / f"{first['id']}.png"
    assert first_path.is_file()

    second_bytes = _png_bytes(color=(9, 8, 7), size=(6, 6))
    second = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("two.png", second_bytes, "image/png")},
    ).json()["attachment"]

    assert second["id"] != first["id"]
    assert not first_path.exists(), "replacing an attachment must delete the old file"
    files = _attachment_files(tmp_path)
    assert len(files) == 1 and files[0].name == f"{second['id']}.png"
    assert _comments(tmp_path)[0]["attachment"]["id"] == second["id"]


def test_replacing_an_attachment_with_a_malformed_old_id_does_not_glob_delete_other_files(
    client: TestClient, tmp_path: Path
) -> None:
    """PR #1425 P3: `_delete_attachment_file` must validate the old
    attachment id's shape before globbing for deletion. The id it globs on
    is read back out of comments.json, not one just minted - a hand-edited
    file, a future writer, or a restored backup could leave a `*`/`?` in it,
    and an unguarded glob would then delete unrelated attachments with no
    bound on the blast radius."""
    pin = _create(client)
    comments_path = tmp_path / "feedback" / "comments.json"
    attachments_dir = tmp_path / "feedback" / "attachments"
    attachments_dir.mkdir(parents=True)

    # Crafted so an unguarded `glob(f"{malformed_id}.*")` matches it.
    unrelated = attachments_dir / "deadbeef0000.png"
    unrelated.write_bytes(_png_bytes())
    malformed_id = "*"

    data = json.loads(comments_path.read_text())
    data["comments"][0]["attachment"] = {
        "id": malformed_id,
        "content_type": "image/png",
        "size_bytes": 1,
        "url": f"/api/v1/feedback/attachments/{malformed_id}",
    }
    comments_path.write_text(json.dumps(data))

    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("new.png", _png_bytes(color=(9, 9, 9)), "image/png")},
    )
    assert r.status_code == 201, r.text
    assert unrelated.exists(), "a malformed old attachment id must not glob-delete unrelated files"


# ----- fetch ------------------------------------------------------------------
def test_fetching_an_unknown_attachment_id_is_404(client: TestClient) -> None:
    r = client.get("/api/v1/feedback/attachments/000000000000")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ATTACHMENT_NOT_FOUND"


@pytest.mark.parametrize(
    "bad_id",
    ["not-hex-chars", "toolong00000000000000", "short0", "UPPERCASE000"],
)
def test_fetching_a_malformed_attachment_id_is_a_clean_404_not_a_crash(
    client: TestClient, bad_id: str
) -> None:
    # None of these match _ATTACHMENT_ID_RE, so they must never reach glob()
    # as a path pattern - a clean 404, not a 500 or a filesystem surprise.
    r = client.get(f"/api/v1/feedback/attachments/{bad_id}")
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "ATTACHMENT_NOT_FOUND"


pytestmark = pytest.mark.rb_parity
