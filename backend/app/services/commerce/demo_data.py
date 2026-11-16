"""Static offline demo dataset for commerce purchase options.

Single Source of Truth (SSOT) for demo items aligned with
data/demo_household_manifest.json (flour, detergent, toothpaste, tissue, milk, soy sauce).
"""

from datetime import datetime, timezone
from urllib.parse import quote

from app.models.commerce import PurchaseOption


def get_static_demo_options(keyword: str, limit: int = 5) -> list[PurchaseOption]:
    """Provide deterministic offline demo recommendations for known demo items.

    Ensures that depleted items in demo-household (laundry detergent, flour, toothpaste,
    tissue, soy sauce, milk) have realistic fallback options without live external APIs.
    """
    options: list[PurchaseOption] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    norm_keyword = keyword.strip().lower()

    if "衛生紙" in norm_keyword or "面紙" in norm_keyword:
        url_1 = f"https://biggo.com.tw/s/?q={quote('舒潔 頂級三層抽取衛生紙', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="舒潔 頂級三層抽取衛生紙",
                raw_title="舒潔 頂級三層抽取衛生紙 100抽x24包",
                store="momo購物網",
                source="demo",
                estimated_price=389.0,
                product_url=url_1,
                unit_spec="100抽x24包",
                image_url="https://img.biggo.com.tw/demo_tissue.jpg",
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('五月花 蓬厚柔抽取式衛生紙', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="五月花 蓬厚柔抽取式衛生紙",
                raw_title="五月花 蓬厚柔抽取式衛生紙 110抽x24包",
                store="PChome 24h購物",
                source="demo",
                estimated_price=349.0,
                product_url=url_2,
                unit_spec="110抽x24包",
                image_url="https://img.biggo.com.tw/demo_mayflower.jpg",
                updated_at=now_iso,
            )
        )
        url_3 = f"https://biggo.com.tw/s/?q={quote('蒲公英 環保抽取式衛生紙', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-3",
                title="蒲公英 環保抽取式衛生紙",
                raw_title="蒲公英 環保抽取式衛生紙 100抽x24包",
                store="蝦皮商城",
                source="demo",
                estimated_price=299.0,
                product_url=url_3,
                unit_spec="100抽x24包",
                image_url=None,
                updated_at=now_iso,
            )
        )
    elif "牛奶" in norm_keyword or "鮮奶" in norm_keyword or "乳" in norm_keyword:
        url_1 = f"https://biggo.com.tw/s/?q={quote('光泉 全脂鮮乳', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="光泉 全脂鮮乳",
                raw_title="光泉 全脂鮮乳 936ml x 2瓶",
                store="家樂福線上購物",
                source="demo",
                estimated_price=185.0,
                product_url=url_1,
                unit_spec="936ml x 2瓶",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('義美 全脂鮮乳', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="義美 全脂鮮乳",
                raw_title="義美 全脂鮮乳 1000ml",
                store="momo購物網",
                source="demo",
                estimated_price=98.0,
                product_url=url_2,
                unit_spec="1000ml",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_3 = f"https://biggo.com.tw/s/?q={quote('瑞穗 全脂鮮乳', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-3",
                title="瑞穗 全脂鮮乳",
                raw_title="瑞穗 全脂鮮乳 930ml",
                store="全聯線上購",
                source="demo",
                estimated_price=92.0,
                product_url=url_3,
                unit_spec="930ml",
                image_url=None,
                updated_at=now_iso,
            )
        )
    elif "醬油" in norm_keyword:
        url_1 = f"https://biggo.com.tw/s/?q={quote('金蘭 純釀造醬油', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="金蘭 純釀造醬油",
                raw_title="金蘭 純釀造醬油 590ml",
                store="全聯線上購",
                source="demo",
                estimated_price=65.0,
                product_url=url_1,
                unit_spec="590ml",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('萬家香 大安純釀造醬油', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="萬家香 大安純釀造醬油",
                raw_title="萬家香 大安純釀造醬油 1000ml",
                store="momo購物網",
                source="demo",
                estimated_price=79.0,
                product_url=url_2,
                unit_spec="1000ml",
                image_url=None,
                updated_at=now_iso,
            )
        )
    elif "洗衣" in norm_keyword or "洗衣精" in norm_keyword:
        url_1 = f"https://biggo.com.tw/s/?q={quote('一匙靈 抗菌 EX 濃縮洗衣精 補充包', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="一匙靈 抗菌 EX 濃縮洗衣精 補充包",
                raw_title="一匙靈 抗菌 EX 濃縮洗衣精 補充包 1.5kg",
                store="momo購物網",
                source="demo",
                estimated_price=89.0,
                product_url=url_1,
                unit_spec="1.5kg",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('白蘭 含熊寶貝馨香洗衣精', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="白蘭 含熊寶貝馨香洗衣精",
                raw_title="白蘭 含熊寶貝馨香洗衣精 2.8kg",
                store="全聯線上購",
                source="demo",
                estimated_price=99.0,
                product_url=url_2,
                unit_spec="2.8kg",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_3 = f"https://biggo.com.tw/s/?q={quote('花王 植萃漂白洗衣精', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-3",
                title="花王 植萃漂白洗衣精",
                raw_title="花王 植萃漂白洗衣精 2000g",
                store="PChome 24h購物",
                source="demo",
                estimated_price=119.0,
                product_url=url_3,
                unit_spec="2000g",
                image_url=None,
                updated_at=now_iso,
            )
        )
    elif any(flour_kw in norm_keyword for flour_kw in ("麵粉", "中筋", "高筋", "低筋")):
        url_1 = f"https://biggo.com.tw/s/?q={quote('日正 中筋麵粉', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="日正 中筋麵粉",
                raw_title="日正 中筋麵粉 1kg",
                store="全聯線上購",
                source="demo",
                estimated_price=55.0,
                product_url=url_1,
                unit_spec="1kg",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('洽發 卡羅低筋麵粉', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="洽發 卡羅低筋麵粉",
                raw_title="洽發 卡羅低筋麵粉 1kg",
                store="momo購物網",
                source="demo",
                estimated_price=68.0,
                product_url=url_2,
                unit_spec="1kg",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_3 = f"https://biggo.com.tw/s/?q={quote('聯華 水手牌特級強力粉', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-3",
                title="聯華 水手牌特級強力粉",
                raw_title="聯華 水手牌特級強力粉 1kg",
                store="家樂福線上購物",
                source="demo",
                estimated_price=75.0,
                product_url=url_3,
                unit_spec="1kg",
                image_url=None,
                updated_at=now_iso,
            )
        )
    elif "牙膏" in norm_keyword:
        url_1 = f"https://biggo.com.tw/s/?q={quote('好來 超氟牙膏', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-1",
                title="好來 超氟牙膏",
                raw_title="好來 超氟牙膏 175g x 2入",
                store="全聯線上購",
                source="demo",
                estimated_price=99.0,
                product_url=url_1,
                unit_spec="175g x 2入",
                image_url=None,
                updated_at=now_iso,
            )
        )
        url_2 = f"https://biggo.com.tw/s/?q={quote('高露潔 抗敏專家牙膏', safe='')}"
        options.append(
            PurchaseOption(
                option_id="demo-opt-2",
                title="高露潔 抗敏專家牙膏",
                raw_title="高露潔 抗敏專家牙膏 110g",
                store="momo購物網",
                source="demo",
                estimated_price=139.0,
                product_url=url_2,
                unit_spec="110g",
                image_url=None,
                updated_at=now_iso,
            )
        )
    else:
        # Unknown keyword returns empty list, adhering to Issue #139 (zero fabricated data)
        return []

    # Ensure options are strictly ordered by estimated_price ascending
    priced_options = sorted(
        [opt for opt in options if opt.estimated_price is not None],
        key=lambda opt: opt.estimated_price,  # type: ignore[arg-type, return-value]
    )
    unpriced_options = [opt for opt in options if opt.estimated_price is None]
    sorted_options = priced_options + unpriced_options

    # Re-assign deterministic sequential IDs: demo-opt-1, demo-opt-2, ... using model_copy
    final_options: list[PurchaseOption] = []
    for idx, opt in enumerate(sorted_options[:limit], start=1):
        final_options.append(opt.model_copy(update={"option_id": f"demo-opt-{idx}"}))

    return final_options
