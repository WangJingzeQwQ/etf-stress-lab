from backend.data_pipeline.trading_rule_evidence import (
    classify_etf_product,
    extract_text_evidence,
    is_twenty_percent_fund,
    select_rule_documents,
)


def test_extract_text_evidence_handles_t1_and_ten_percent_limit():
    evidence = extract_text_evidence(
        "基金实行价格涨跌幅限制，涨跌幅比例为10%，自上市首日起实行。"
        "当日买入的基金份额，同日可以赎回，但不得卖出。"
    )
    assert [item["settlement"] for item in evidence["settlement_hints"]] == ["T1"]
    assert [item["limit_pct"] for item in evidence["limit_hints"]] == ["0.1000"]


def test_extract_text_evidence_handles_t0_and_twenty_percent_limit():
    evidence = extract_text_evidence(
        "本基金竞价交易也实行20%涨跌幅比例限制。"
        "当日买入的基金份额，同日可以卖出或赎回。"
    )
    assert [item["settlement"] for item in evidence["settlement_hints"]] == ["T0"]
    assert [item["limit_pct"] for item in evidence["limit_hints"]] == ["0.2000"]


def test_product_classification_and_twenty_percent_markers():
    assert classify_etf_product("中证港股通科技交易型开放式指数证券投资基金") == (
        "crossborder"
    )
    assert classify_etf_product("中证AAA科技创新公司债交易型开放式指数证券投资基金") == (
        "bond"
    )
    assert classify_etf_product("华安黄金交易型开放式证券投资基金") == "gold"
    assert classify_etf_product("华夏饲料豆粕期货交易型开放式指数证券投资基金") == (
        "commodity"
    )
    assert is_twenty_percent_fund("华夏上证科创板50交易型开放式指数证券投资基金")
    assert is_twenty_percent_fund("易方达创业板交易型开放式指数证券投资基金")
    assert not is_twenty_percent_fund("华泰柏瑞沪深300交易型开放式指数证券投资基金")


def test_product_classification_handles_short_names_and_issuer_fragments():
    assert (
        classify_etf_product(
            "华安国际龙头(DAX)交易型开放式指数证券投资基金"
        )
        == "crossborder"
    )
    assert (
        classify_etf_product("上海金交易型开放式证券投资基金")
        == "gold"
    )
    assert (
        classify_etf_product("兴证全球沪深300质量交易型开放式指数证券投资基金")
        == "stock"
    )
    assert classify_etf_product("国债ETF招募说明书更新") == "bond"
    assert classify_etf_product("有色期货") == "commodity"


def test_select_rule_documents_keeps_earliest_latest_listing_and_change():
    announcements = [
        {
            "ID": "OLD",
            "FUNDCODE": "510300",
            "TITLE": "基金招募说明书",
            "PUBLISHDATE": "2018-01-02T00:00:00",
        },
        {
            "ID": "LIST",
            "FUNDCODE": "510300",
            "TITLE": "基金上市交易公告书",
            "PUBLISHDATE": "2018-02-01T00:00:00",
        },
        {
            "ID": "CHANGE",
            "FUNDCODE": "510300",
            "TITLE": "关于调整申赎结算模式的公告",
            "PUBLISHDATE": "2021-07-20T00:00:00",
        },
        {
            "ID": "NEW",
            "FUNDCODE": "510300",
            "TITLE": "基金招募说明书更新",
            "PUBLISHDATE": "2026-01-01T00:00:00",
        },
    ]
    selected, _ = select_rule_documents(announcements)
    assert {item["id"] for item in selected} == {"OLD", "LIST", "CHANGE", "NEW"}
