import pytest

from scripts.skins.layout_iou import layout_iou


def test_identical_boxes_iou_one():
    r = layout_iou({"a": [0, 0, 10, 10]}, {"a": [0, 0, 10, 10]})
    assert r.per_region["a"] == 1.0
    assert r.mean == 1.0


def test_disjoint_boxes_iou_zero():
    r = layout_iou({"a": [0, 0, 10, 10]}, {"a": [20, 20, 10, 10]})
    assert r.per_region["a"] == 0.0


def test_missing_region_scores_zero_and_listed():
    r = layout_iou({"a": [0, 0, 10, 10], "b": [0, 0, 5, 5]}, {"a": [0, 0, 10, 10]})
    assert r.per_region["b"] == 0.0
    assert r.missing == ["b"]
    assert r.mean == 0.5


@pytest.mark.parametrize("box", [[0, 0, 0, 5], [0, 0, 5, -1]])
def test_nonpositive_size_raises_naming_region(box):
    with pytest.raises(ValueError, match="deck_left"):
        layout_iou({"deck_left": box}, {})


def test_half_overlap_is_one_third():
    r = layout_iou({"a": [0, 0, 10, 10]}, {"a": [5, 0, 10, 10]})
    assert r.per_region["a"] == pytest.approx(1 / 3, abs=1e-9)
