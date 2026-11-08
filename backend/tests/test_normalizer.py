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
        quantity=2,
        unit="瓶",
        capacity_spec="936ml",
    )
    assert item.canonical_name == "全脂鮮乳"
    assert item.quantity == 2
    assert isinstance(item.quantity, int)
    assert item.unit == "瓶"

    # JSON roundtrip
    dumped = item.model_dump_json()
    loaded = NormalizedItem.model_validate_json(dumped)
    assert loaded == item


def test_normalized_item_rejects_unagreed_fields():
    data = {
        "raw_text": "測試",
        "canonical_name": "測試品",
        "feature_keywords": ["測試"],
        "quantity": 1,
        "unit": "個",
        "extra_field": "unagreed",
    }
    with pytest.raises(ValidationError):
        NormalizedItem.model_validate(data)


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
    assert res.quantity == 2
    assert isinstance(res.quantity, int)
    assert res.unit == "瓶"
    assert res.unit_confirmed is True
    assert res.capacity_spec == "936ml"
    # Issue #36 Plan B retires feature_keywords substring splitting and static synonym tables
    assert res.feature_keywords == []
    assert res.aliases == ["義美全脂鮮乳"]


def test_item_normalizer_resolves_multi_piece_box():
    normalizer = ItemNormalizer()
    # 100抽 x 24包
    res = normalizer.normalize("舒潔頂級三層抽取式衛生紙 100抽x24包/箱")
    assert res.brand == "舒潔"
    assert "衛生紙" in res.canonical_name
    assert res.quantity == 24
    assert isinstance(res.quantity, int)
    assert res.unit == "包"
    assert res.unit_confirmed is True
    assert res.capacity_spec == "100抽"
    assert "舒潔" not in res.feature_keywords


def test_item_normalizer_resolves_coca_cola_pack():
    normalizer = ItemNormalizer()
    res = normalizer.normalize("可口可樂 330ml 6入組")
    assert res.brand == "可口可樂"
    assert res.quantity == 6
    assert isinstance(res.quantity, int)
    assert res.unit == "入"
    assert res.unit_confirmed is True
    assert res.capacity_spec == "330ml"
    assert "可口可樂" not in res.feature_keywords


def test_item_normalizer_fallback_defaults_leaves_unit_unconfirmed():
    normalizer = ItemNormalizer()
    # 當品名中無可解析量詞或規格時，不強加「個」，回傳 None 並標記 unit_confirmed=False
    res = normalizer.normalize("蘋果")
    assert res.canonical_name == "蘋果"
    assert res.quantity == 1
    assert isinstance(res.quantity, int)
    assert res.unit is None
    assert res.unit_confirmed is False
    assert res.feature_keywords == []


def test_item_normalizer_explicit_fallback_unit():
    normalizer = ItemNormalizer()
    res = normalizer.normalize("蘋果", fallback_unit="個")
    assert res.unit == "個"
    assert res.unit_confirmed is True


def test_normalized_item_unconfirmed_unit_model():
    item = NormalizedItem(
        raw_text="醬油",
        canonical_name="醬油",
        quantity=1,
        unit=None,
        unit_confirmed=False,
    )
    assert item.unit is None
    assert item.unit_confirmed is False


def test_item_normalizer_rejects_empty_input():
    normalizer = ItemNormalizer()
    with pytest.raises(ValueError):
        normalizer.normalize("   ")


@pytest.mark.parametrize(
    ("raw_text", "expected_canonical", "wrong_truncated"),
    [
        ("萬家香醬油膏 500ml", "醬油膏", "醬油"),
        ("義美100%純蘋果汁 900ml", "100%純蘋果汁", "蘋果"),
        ("日式起司可樂餅 4入", "日式起司可樂餅", "可樂"),
        ("新竹純白米粉 300g", "新竹純白米粉", "白米"),
    ],
)
def test_item_normalizer_preserves_full_canonical_name_without_suffix_truncation(
    raw_text: str, expected_canonical: str, wrong_truncated: str
):
    """Issue #35 & #36 Plan B: retiring 21-token substring splitting prevents false truncation."""
    normalizer = ItemNormalizer()
    res = normalizer.normalize(raw_text)
    assert res.canonical_name == expected_canonical
    assert res.canonical_name != wrong_truncated
    assert res.feature_keywords == []
