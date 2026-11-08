"""
结果格式化器测试（无需 API Key、不访问网络）

运行：python tests/test_result_formatter.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.result_formatter import result_to_markdown


def _wrap(results):
    return {"status": "completed", "results": results}


def test_itinerary():
    data = _wrap([
        {
            "agent_name": "itinerary_planning",
            "priority": 2,
            "status": "success",
            "data": {
                "itinerary": {
                    "title": "北京—杭州3天行程",
                    "duration": "3天",
                    "daily_plans": [
                        {
                            "day": 1,
                            "activities": [
                                {"time": "09:00", "activity": "出发", "description": "高铁", "transport": "地铁"}
                            ],
                            "meals": {"lunch": "简餐"},
                        }
                    ],
                    "notes": ["提前订票"],
                }
            },
        }
    ])
    md = result_to_markdown(data)
    assert "北京—杭州3天行程" in md
    assert "第 1 天" in md
    assert "09:00" in md and "高铁" in md
    assert "提前订票" in md
    print("✓ 行程渲染正常")


def test_preference():
    data = _wrap([
        {
            "agent_name": "preference",
            "priority": 1,
            "status": "success",
            "data": {"preferences": [{"type": "hotel_brands", "value": "汉庭", "action": "replace"}]},
        }
    ])
    md = result_to_markdown(data)
    assert "已更新偏好" in md
    assert "酒店偏好" in md and "汉庭" in md
    print("✓ 偏好更新渲染正常")


def test_rag_answer_with_sources():
    data = _wrap([
        {
            "agent_name": "rag_knowledge",
            "priority": 1,
            "status": "success",
            "data": {
                "answer": "一线城市住宿标准为 500 元/晚。",
                "retrieved_documents": [
                    {
                        "content": "一线城市住宿标准：500 元/晚，凭发票报销。",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    },
                    {
                        "content": "其他城市住宿标准：350 元/晚。",
                        "metadata": {"parent_doc": "01_travel_standards.txt"},
                    },
                ],
            },
        }
    ])
    md = result_to_markdown(data)
    assert "500 元/晚" in md
    assert "参考来源" in md and "01_travel_standards.txt" in md
    assert "参考片段" in md
    assert "凭发票报销" in md and "其他城市" in md
    assert "> " in md
    print("✓ RAG 回答与来源 / 参考片段渲染正常")


def test_rag_malformed_documents():
    data = _wrap([
        {
            "agent_name": "rag_knowledge",
            "status": "success",
            "data": {"answer": "答案", "retrieved_documents": {"oops": 1}},
        }
    ])
    assert result_to_markdown(data) == "答案"
    print("✓ RAG 非列表检索结果安全兜底")


def test_information_query_sources():
    data = _wrap([
        {
            "agent_name": "information_query",
            "priority": 1,
            "status": "success",
            "data": {"results": {"summary": "杭州多云 18℃", "sources": [{"url": "https://open-meteo.com"}]}},
        }
    ])
    md = result_to_markdown(data)
    assert "杭州多云" in md
    assert "open-meteo.com" in md
    print("✓ 信息查询渲染正常")


def test_error_result():
    data = _wrap([
        {"agent_name": "memory_query", "priority": 1, "status": "error", "data": {"error": "服务不可用"}}
    ])
    md = result_to_markdown(data)
    assert "执行失败" in md and "服务不可用" in md
    print("✓ 错误渲染正常")


def test_no_agents():
    md = result_to_markdown({"status": "no_agents", "results": []})
    assert "已记录" in md
    print("✓ 无智能体调度时的提示正常")


def test_itinerary_malformed_shapes_do_not_crash():
    """模型返回非标准行程结构时渲染不能抛异常（否则整轮回答 500）。"""
    payloads = [
        {"itinerary": "以下是为您规划的行程：北京出发，高铁前往杭州。"},
        {"itinerary": ["北京", "上海"]},
        {"itinerary": {"daily_plans": {"第1天": [{"time": "09:00", "activity": "出发"}]}}},
        {
            "itinerary": {
                "daily_plans": [
                    "不是字典的一天",
                    {"day": 1, "activities": "不是列表", "meals": "不是字典"},
                    {"day": 2, "activities": [{"time": "10:00", "activity": "会议"}]},
                ],
                "notes": "带伞",
            }
        },
    ]
    for payload in payloads:
        md = result_to_markdown(
            _wrap(
                [
                    {
                        "agent_name": "itinerary_planning",
                        "priority": 1,
                        "status": "success",
                        "data": payload,
                    }
                ]
            )
        )
        assert md.strip(), payload

    # data 不是对象、status=error 且 data 为字符串
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "itinerary_planning",
                    "priority": 1,
                    "status": "success",
                    "data": ["a", "b"],
                }
            ]
        )
    )
    assert md
    md = result_to_markdown(
        _wrap(
            [{"agent_name": "x", "priority": 1, "status": "error", "data": "服务不可用"}]
        )
    )
    assert "执行失败" in md
    print("✓ 非标准行程结构 / 非对象 data 不崩溃")


def test_event_collection_and_memory_query():
    """事项收集（字段 / 缺失提示）与记忆查询的渲染。"""
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "event_collection",
                    "priority": 1,
                    "status": "success",
                    "data": {
                        "origin": "北京",
                        "destination": "杭州",
                        "start_date": "2026-12-01",
                        "missing_info": ["返程日期", "出行目的"],
                    },
                },
                {
                    "agent_name": "memory_query",
                    "priority": 1,
                    "status": "success",
                    "data": {"answer": "你上次去过上海"},
                },
            ]
        )
    )
    assert "已收集行程信息" in md
    assert "出发地：北京" in md and "目的地：杭州" in md
    assert "还需要补充：返程日期、出行目的" in md
    assert "你上次去过上海" in md

    # 只有缺失提示、没有已收集字段
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "event_collection",
                    "priority": 1,
                    "status": "success",
                    "data": {"missing_info": ["出发地"]},
                }
            ]
        )
    )
    assert md == "💡 还需要补充：出发地"
    print("✓ 事项收集 / 记忆查询渲染")


def test_information_query_variants_and_fallback_agent():
    """信息查询的字符串来源 / 扁平结果，以及未知智能体的通用兜底。"""
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "information_query",
                    "priority": 1,
                    "status": "success",
                    "data": {
                        "summary": "杭州明天小雨",
                        "sources": ["https://example.com/a", {"url": "https://example.com/b"}],
                    },
                },
                {
                    "agent_name": "travel_tips",
                    "priority": 1,
                    "status": "success",
                    "data": {"summary": "建议提前订票"},
                },
            ]
        )
    )
    assert "杭州明天小雨" in md
    assert "1. https://example.com/a" in md
    assert "2. https://example.com/b" in md
    assert "建议提前订票" in md

    # preferences 被包成 {"preferences": [...]} 的形态
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "preference",
                    "priority": 1,
                    "status": "success",
                    "data": {"preferences": {"preferences": [{"type": "home_location", "value": "上海", "action": "append"}]}},
                }
            ]
        )
    )
    assert "常驻地 **追加** 上海" in md
    print("✓ 信息查询来源变体 / 未知智能体兜底 / 偏好包一层")


def test_empty_and_malformed_results():
    """空 / 脏结果：占位提示、非字典条目跳过、无可渲染内容占位。"""
    assert result_to_markdown({"results": []}) == "未能获取有效结果，请重新描述您的需求。"
    assert (
        result_to_markdown({"status": "completed"})
        == "未能获取有效结果，请重新描述您的需求。"
    )
    md = result_to_markdown(
        _wrap(
            [
                None,
                "bad",
                {
                    "agent_name": "memory_query",
                    "status": "success",
                    "data": {"answer": "记起来了"},
                },
            ]
        )
    )
    assert md == "记起来了"
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "preference",
                    "status": "success",
                    "data": {"preferences": []},
                }
            ]
        )
    )
    assert md == "已处理您的请求。"
    print("✓ 空结果 / 非字典条目 / 无可渲染内容")


def test_render_failure_falls_back_to_text():
    """渲染器抛错时兜底提取文本；兜底无文本时返回占位。"""
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "preference",
                    "status": "success",
                    "data": {
                        # 不可哈希的 type 让 preference_label 抛 TypeError
                        "preferences": [{"type": {"bad": 1}}],
                        "message": "已更新偏好（降级展示）",
                    },
                }
            ]
        )
    )
    assert md == "已更新偏好（降级展示）"

    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "preference",
                    "status": "success",
                    "data": {"preferences": [{"type": {"bad": 1}}]},
                }
            ]
        )
    )
    assert md == "已处理您的请求。"

    from utils.result_formatter import _fallback_text

    assert _fallback_text("  文本  ") == "文本"
    assert _fallback_text({"summary": "S"}) == "S"
    assert _fallback_text({}) == ""
    print("✓ 渲染失败兜底文本")


def test_itinerary_string_and_notes_variants():
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "itinerary_planning",
                    "status": "success",
                    "data": {"itinerary": " 北京三日游 "},
                }
            ]
        )
    )
    assert "北京三日游" in md

    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "itinerary_planning",
                    "status": "success",
                    "data": {
                        "itinerary": {
                            "notes": "带伞",
                            "daily_plans": [{"day": 1, "meals": {"dinner": "火锅"}}],
                        }
                    },
                }
            ]
        )
    )
    assert "带伞" in md and "火锅" in md

    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "itinerary_planning",
                    "status": "success",
                    "data": {"error": "缺少目的地"},
                }
            ]
        )
    )
    assert "缺少目的地" in md
    print("✓ 行程字符串 / 字符串 notes / 行程 error 渲染")


def test_agent_specific_empty_payloads_render_placeholder():
    cases = [
        {"agent_name": "event_collection", "status": "success", "data": {}},
        {"agent_name": "information_query", "status": "success", "data": {}},
        {"agent_name": "rag_knowledge", "status": "success", "data": {"answer": ""}},
        {"agent_name": "memory_query", "status": "success", "data": {}},
    ]
    assert result_to_markdown(_wrap(cases)) == "已处理您的请求。"
    # 空偏好条目（非 dict / value 为 null）不会落库，回显也不能假装「已更新」
    for empty_prefs in ([None], [{"type": "hotel_brands", "value": None}]):
        md = result_to_markdown(
            _wrap(
                [
                    {
                        "agent_name": "preference",
                        "status": "success",
                        "data": {"preferences": empty_prefs},
                    }
                ]
            )
        )
        assert md == "已处理您的请求。", (empty_prefs, md)
    print("✓ 各智能体空载荷渲染占位")


def test_rag_snippet_truncation_and_skip_non_dict():
    long_text = "标准" * 100  # 200 字，超过 140 字截断阈值
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "rag_knowledge",
                    "status": "success",
                    "data": {
                        "answer": "答案",
                        "retrieved_documents": [
                            "坏文档",
                            {"content": ""},
                            {"content": long_text, "metadata": {"title": "制度.txt"}},
                        ],
                    },
                }
            ]
        )
    )
    assert "制度.txt" in md
    assert "…" in md, "超长片段应截断并加省略号"
    assert "坏文档" not in md, "非字典文档不参与渲染"
    print("✓ RAG 片段截断 / 非字典文档跳过")


def test_preference_append_variants_and_legacy_dict():
    """Append / add / 追加 都渲染为「追加」；旧格式 {type: value} 字典不再渲染为空。"""
    for action in ("Append", "add", "追加", "append"):
        data = _wrap([
            {
                "agent_name": "preference",
                "priority": 1,
                "status": "success",
                "data": {
                    "preferences": [
                        {"type": "hotel_brands", "value": ["如家"], "action": action}
                    ]
                },
            }
        ])
        md = result_to_markdown(data)
        assert "追加" in md, f"action={action} 应渲染为追加"
        assert "['如家']" not in md and "如家" in md

    legacy = _wrap([
        {
            "agent_name": "preference",
            "priority": 1,
            "status": "success",
            "data": {
                "preferences": {
                    "hotel_brands": ["汉庭", "如家"],
                    "home_location": "上海",
                }
            },
        }
    ])
    md = result_to_markdown(legacy)
    assert "汉庭" in md and "如家" in md and "上海" in md
    assert "['汉庭'" not in md
    print("✓ 偏好追加变体与旧格式字典渲染")


def test_event_collection_type_tolerance():
    """missing_info 为字符串不能拆成单字；列表字段不输出 Python repr。"""
    data = _wrap([
        {
            "agent_name": "event_collection",
            "priority": 1,
            "status": "success",
            "data": {"destination": ["上海", "杭州"], "missing_info": "出发日期"},
        }
    ])
    md = result_to_markdown(data)
    assert "上海、杭州" in md
    assert "['上海'" not in md
    assert "出发日期" in md and "出、发、日、期" not in md

    nested_bad = _wrap([
        {
            "agent_name": "event_collection",
            "priority": 1,
            "status": "success",
            "data": {"data": "not-a-dict", "origin": "北京"},
        }
    ])
    assert "北京" in result_to_markdown(nested_bad)
    print("✓ 事项收集字段类型容错")


def test_null_values_render_defaults_not_none():
    """模型输出显式 null：偏好跳过（不会落库）、行程回退默认文案，不出现字面量 None。"""
    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "preference",
                    "status": "success",
                    "data": {
                        "preferences": [
                            {"type": "hotel_brands", "value": ["汉庭", None]},
                            {"type": "budget_level", "value": None},
                        ]
                    },
                }
            ]
        )
    )
    assert "汉庭" in md and "None" not in md, md

    md = result_to_markdown(
        _wrap(
            [
                {
                    "agent_name": "itinerary_planning",
                    "status": "success",
                    "data": {
                        "itinerary": {
                            "title": None,
                            "daily_plans": [
                                {
                                    "day": None,
                                    "activities": [{"time": None, "activity": "出发"}],
                                }
                            ],
                        }
                    },
                }
            ]
        )
    )
    assert "## ✈️ 行程规划" in md, md
    assert "第 ? 天" in md, md
    assert "None" not in md, md
    print("✓ 显式 null 渲染回退（偏好跳过 / 行程默认文案）")


def test_preference_nested_data_and_empty_legacy_values():
    """data.data 嵌套要回显；旧格式空值不能宣称已更新（与落库同口径）。"""
    nested = _wrap(
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "status": "success",
                "data": {
                    "data": {
                        "preferences": [{"type": "airlines", "value": "东航"}]
                    }
                },
            }
        ]
    )
    md = result_to_markdown(nested)
    assert "东航" in md, md
    assert "已处理您的请求" not in md, md

    legacy = _wrap(
        [
            {
                "agent_name": "preference",
                "priority": 1,
                "status": "success",
                "data": {
                    "preferences": {"airlines": "东航", "seat_preference": ""}
                },
            }
        ]
    )
    md = result_to_markdown(legacy)
    assert "东航" in md, md
    assert "座位偏好" not in md, "空值不落库，回显也不能宣称已更新"
    print("✓ 偏好嵌套与空值回显（与落库同口径）")


if __name__ == "__main__":
    test_itinerary()
    test_preference()
    test_rag_answer_with_sources()
    test_rag_malformed_documents()
    test_information_query_sources()
    test_error_result()
    test_no_agents()
    test_itinerary_malformed_shapes_do_not_crash()
    test_event_collection_and_memory_query()
    test_information_query_variants_and_fallback_agent()
    test_empty_and_malformed_results()
    test_render_failure_falls_back_to_text()
    test_itinerary_string_and_notes_variants()
    test_agent_specific_empty_payloads_render_placeholder()
    test_rag_snippet_truncation_and_skip_non_dict()
    test_preference_append_variants_and_legacy_dict()
    test_preference_nested_data_and_empty_legacy_values()
    test_event_collection_type_tolerance()
    test_null_values_render_defaults_not_none()
    print("\n全部通过")
