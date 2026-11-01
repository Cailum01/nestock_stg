import pytest
from pydantic import ValidationError

from app.models.normalization import NormalizedItem
from app.services.normalizer import ItemNormalizer


def test_normalized_item_model_validation():
    item = NormalizedItem(
        raw_text="【限時特價】義美全脂鮮乳 936ml 2瓶裝",
        canonical_name="全脂鮮乳",
        brand="義美",
        feature_keywords=["鮮乳", "全脂", "義美"],
        aliases=["義美鮮乳", "鮮奶"],
        quantity=2.0,
        unit="瓶",
        capacity_spec="936ml",
    )
    assert item.canonical_name == "全脂鮮乳"
    assert item.quantity == 2.0
    assert item.unit == "瓶"

    # JSON roundtrip
    dumped = item.model_dump_json()
    loaded = NormalizedItem.model_validate_json(dumped)
    assert loaded == item


def test_normalized_item_rejects_unagreed_fields():
    with pytest.raises(ValidationError):
        NormalizedItem(
            raw_text="測試",
            canonical_name="測試品",
            feature_keywords=["測試"],
            quantity=1.0,
            unit="個",
            extra_field="unagreed",
        )


def test_item_normalizer_strips_marketing_noise():
    normalizer = ItemNormalizer()
    text = "【限時特價】義美全脂鮮乳 買一送一 超值家庭號"
    cleaned = normalizer.strip_marketing_noise(text)
    assert "限時特價" not in cleaned
    assert "【】" not in cleaned
    assert "買一送一" not in cleaned
    assert "超值家庭號" not in cleaned
    assert "義美全脂鮮乳" in cleaned


def test_item_normalizer_resolves_single_package_and_capacity():
    normalizer = ItemNormalizer()
    res = normalizer.normalize("【特價】義美全脂鮮乳 936ml 2瓶裝")
    assert res.brand == "義美"
    assert res.canonical_name == "全脂鮮乳"
    assert res.quantity == 2.0
    assert res.unit == "瓶"
    assert res.capacity_spec == "936ml"
    assert "鮮乳" in res.feature_keywords
    assert "義美" in res.feature_keywords


def test_item_normalizer_resolves_multi_piece_box():
    normalizer = ItemNormalizer()
    # 100抽 x 24包
    res = normalizer.normalize("舒潔頂級三層抽取式衛生紙 100抽x24包/箱")
    assert res.brand == "舒潔"
    assert "衛生紙" in res.canonical_name
    assert res.quantity == 24.0
    assert res.unit == "包"
    assert res.capacity_spec == "100抽"


def test_item_normalizer_resolves_coca_cola_pack():
    normalizer = ItemNormalizer()
    res = normalizer.normalize("可口可樂 330ml 6入組")
    assert res.brand == "可口可樂"
    assert res.quantity == 6.0
    assert res.unit == "入"
    assert res.capacity_spec == "330ml"


def test_item_normalizer_fallback_defaults():
    normalizer = ItemNormalizer()
    res = normalizer.normalize("蘋果")
    assert res.canonical_name == "蘋果"
    assert res.quantity == 1.0
    assert res.unit == "個"
    assert "蘋果" in res.feature_keywords


def test_item_normalizer_rejects_empty_input():
    normalizer = ItemNormalizer()
    with pytest.raises(ValueError):
        normalizer.normalize("   ")
