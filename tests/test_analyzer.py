# -*- coding: utf-8 -*-
"""analyzer 分类归一: LLM 会回编号/近义词/非法值, 必须落到六类之一或如实原样返回

真实现场 (2026-10-28 全量 --llm): qwen 有 2 条把 prompt 里的编号 "5" 当分类回,
归一器不认识数字就原样透传, 评测把这 2 条判成错, 还会污染下游 blame 投影。
"""
from ai_kit import analyzer


def test_all_six_ordinals_map_to_the_six_categories():
    mapping = {
        "1": analyzer.CATEGORY_SCRIPT_HEALER,
        "2": analyzer.CATEGORY_SCRIPT_WAIT,
        "3": analyzer.CATEGORY_SYNTAX,
        "4": analyzer.CATEGORY_ENV,
        "5": analyzer.CATEGORY_PRODUCT_BUG,
        "6": analyzer.CATEGORY_UNKNOWN,
    }
    for raw, expected in mapping.items():
        assert analyzer.normalize_category(raw) == expected


def test_ordinal_variants_are_accepted():
    assert analyzer.normalize_category("5") == analyzer.CATEGORY_PRODUCT_BUG
    assert analyzer.normalize_category(5) == analyzer.CATEGORY_PRODUCT_BUG
    assert analyzer.normalize_category("第5类") == analyzer.CATEGORY_PRODUCT_BUG
    assert analyzer.normalize_category(" 5. ") == analyzer.CATEGORY_PRODUCT_BUG


def test_ordinal_match_is_whole_string_only():
    # 含数字但不是纯编号的文本不能被当成编号: 走关键词/原样返回, 不能误判成"产品缺陷"
    assert analyzer.normalize_category("5 个元素找不到") == "5 个元素找不到"
    assert analyzer.normalize_category("5 个元素找不到") != analyzer.CATEGORY_PRODUCT_BUG


def test_chinese_keywords_still_win():
    assert analyzer.normalize_category("脚本-定位器问题") == analyzer.CATEGORY_SCRIPT_HEALER
    assert analyzer.normalize_category("等待超时") == analyzer.CATEGORY_SCRIPT_WAIT
    assert analyzer.normalize_category("环境问题") == analyzer.CATEGORY_ENV
    assert analyzer.normalize_category("产品缺陷") == analyzer.CATEGORY_PRODUCT_BUG


def test_unrecognized_value_is_returned_as_is():
    # 原样返回是既有契约: 由调用方 (blame 投影) 决定归到"未知", 不在这里瞎猜
    assert analyzer.normalize_category("奇怪的说法") == "奇怪的说法"
    assert analyzer.normalize_category("") == ""
