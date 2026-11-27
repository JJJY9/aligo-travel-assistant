"""行程约束校验测试（纯 Python，无需模型 / Milvus）。

覆盖：需求提取（预算 / 必去项 / 天数）、五项校验、问题格式化。

运行：python tests/test_itinerary_validation.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.itinerary_validation import (
    extract_requirements,
    format_issues,
    validate_itinerary,
)


def test_extract_requirements_budget_must_see_duration():
    """从自然语言里提取预算、必去项（含「和」拆分）与天数。"""
    requirements = extract_requirements(
        "下周三去杭州玩3天，预算5000元，一定要去西湖和灵隐寺逛逛"
    )
    assert requirements["budget"] == 5000
    assert requirements["duration_days"] == 3
    assert "西湖" in requirements["must_see"]
    assert "灵隐寺" in requirements["must_see"]
    print("✓ 需求提取：预算 / 必去项 / 天数")


def test_extract_requirements_wan_and_chinese_duration():
    """万元预算与中文天数归一。"""
    requirements = extract_requirements("预算 2 万元，玩三天，必去故宫")
    assert requirements["budget"] == 20000
    assert requirements["duration_days"] == 3
    assert requirements["must_see"] == ["故宫"]
    print("✓ 万元预算 / 中文天数")


def test_date_day_is_not_duration_and_dates_drive_day_count():
    """「3月4日」的「4日」不是天数；起止日期完整时按日期算天数。"""
    requirements = extract_requirements("3月4日出发去杭州玩3天")
    assert requirements["duration_days"] == 3
    requirements = extract_requirements(
        "",
        {
            "start_date": "2026-03-04",
            "end_date": "2026-03-06",
            "duration_days": 5,
        },
    )
    assert requirements["duration_days"] == 3, "应按起止日期算天数（含首尾）"
    print("✓ 日期不再被当作天数，起止日期决定天数")


def test_budget_does_not_cross_clause_punctuation():
    """预算关键词不能越过逗号 / 句号去匹配后面无关的金额。"""
    assert "budget" not in extract_requirements("预算还没定，酒店 500 元")
    assert extract_requirements("预算大概是5000元")["budget"] == 5000
    print("✓ 预算提取不跨标点")


def test_must_see_trims_duration_and_budget_fragments():
    """必去项捕获里的天数 / 预算片段要截掉，不能变成完整地名。"""
    requirements = extract_requirements("必去西湖玩3天，预算5000元")
    assert requirements["must_see"] == ["西湖"], requirements
    requirements = extract_requirements("想去故宫预算5000")
    assert requirements["must_see"] == ["故宫"], requirements
    assert requirements["budget"] == 5000
    print("✓ 必去项截掉天数 / 预算片段")


def test_must_see_skips_narrative_and_preserves_connector_names():
    """「北京旅游」「景点有哪些」不是地名；颐和园 / 和平饭店不能被拆坏。"""
    assert extract_requirements("我想去北京旅游")["must_see"] == ["北京"]
    assert "must_see" not in extract_requirements("北京必去景点有哪些")
    assert extract_requirements("想去颐和园")["must_see"] == ["颐和园"]
    assert extract_requirements("必去和平饭店")["must_see"] == ["和平饭店"]
    assert extract_requirements("必去颐和园和圆明园")["must_see"] == [
        "颐和园",
        "圆明园",
    ]
    print("✓ 必去项保护连接字地名并过滤叙述词")


def test_must_see_strips_destination_and_purpose():
    """出行目的（北京出差）不是景点；城市前缀后的景点保留。"""
    requirements = extract_requirements(
        "想去北京出差3天", {"destination": "北京市"}
    )
    assert "must_see" not in requirements, requirements
    requirements = extract_requirements(
        "想去杭州西湖游玩", {"destination": "杭州市"}
    )
    assert requirements["must_see"] == ["西湖"], requirements
    print("✓ 必去项剥离城市前缀与出行目的")


def test_budget_does_not_cross_enumeration_punctuation():
    """顿号 / 冒号后的分项金额不能被当成总预算。"""
    assert "budget" not in extract_requirements(
        "预算还没定、酒店500元左右"
    )
    assert "budget" not in extract_requirements("费用：交通500，住宿600")
    assert extract_requirements("预算：5000元")["budget"] == 5000
    print("✓ 预算不跨顿号 / 冒号")


def test_must_see_only_checks_daily_plans():
    """备注里提到的地名不算已安排：必须出现在每日行程里。"""
    itinerary = {
        "notes": "本行程参考雷峰塔夜景攻略",
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖"}
                ],
            }
        ],
    }
    issues = validate_itinerary(itinerary, {"must_see": ["雷峰塔"]})
    assert [issue["code"] for issue in issues] == ["must_see_missing"], issues
    print("✓ 必去项只核对每日安排")


def test_validate_passes_for_consistent_plan():
    """天数、时间、预算、必去项、费用一致时无问题。"""
    itinerary = {
        "estimated_budget": 900,
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 600},
                    {"time": "13:00-17:00", "title": "灵隐寺", "cost": 300},
                ],
            }
        ],
    }
    requirements = {
        "duration_days": 1,
        "budget": 1000,
        "must_see": ["西湖", "灵隐寺"],
    }
    assert validate_itinerary(itinerary, requirements) == []
    print("✓ 合规行程五项校验通过")


def test_validate_detects_all_five_checks():
    """天数 / 时间冲突 / 预算 / 必去项 / 费用异常都能被检出。"""
    itinerary = {
        "estimated_budget": 5000,
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 1000},
                    {"time": "11:00-13:00", "title": "灵隐寺", "cost": -5},
                    {"time": "15:00-09:00", "title": "夜游", "cost": 100},
                ],
            }
        ],
    }
    requirements = {
        "duration_days": 2,
        "budget": 1000,
        "must_see": ["雷峰塔"],
    }
    issues = validate_itinerary(itinerary, requirements)
    codes = {issue["code"] for issue in issues}
    assert {
        "day_count_mismatch",
        "time_conflict",
        "invalid_time_range",
        "budget_exceeded",
        "must_see_missing",
        "cost_anomaly",
        "cost_mismatch",
    } <= codes, issues
    assert "雷峰塔" in format_issues(issues)
    print("✓ 五项校验：天数 / 时间 / 预算 / 必去项 / 费用")


def test_validate_handles_empty_and_unknown_shapes():
    """空行程 / 非对象输入返回明确问题而不是抛异常。"""
    assert validate_itinerary(None) [0]["code"] == "missing_itinerary"
    assert validate_itinerary({})[0]["code"] == "missing_daily_plans"
    assert validate_itinerary({"daily_plans": []})[0]["code"] == "missing_daily_plans"
    print("✓ 空行程 / 非法形状的兜底")


def test_must_see_enumeration_with_separators():
    """顿号 / 逗号枚举的必去项全部捕获（此前只取第一个）。"""
    requirements = extract_requirements(
        "去杭州玩3天，必去西湖、灵隐寺、雷峰塔，预算5000元"
    )
    assert requirements["budget"] == 5000, requirements
    assert requirements["must_see"] == ["西湖", "灵隐寺", "雷峰塔"], requirements
    # 同一句里的千分位预算不能把「10,000」拆成幽灵必去项「000元」
    mixed = extract_requirements(
        "去杭州玩3天，必去西湖、灵隐寺和雷峰塔，预算10,000元"
    )
    assert mixed["budget"] == 10000, mixed
    assert mixed["must_see"] == ["西湖", "灵隐寺", "雷峰塔"], mixed
    plan = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-11:00", "title": "西湖", "cost": 1000}
                ],
                "estimated_budget": 1000,
            }
        ]
    }
    issues = validate_itinerary(plan, requirements)
    assert any(issue["code"] == "must_see_missing" for issue in issues), issues
    print("✓ 顿号枚举的必去项全部参与校验")


def test_must_see_ignores_negated_places():
    """「不想去 / 不太想去 / 不一定要去」里的地点不是必去项。"""
    assert extract_requirements("我不想去西湖，想去灵隐寺")["must_see"] == ["灵隐寺"]
    assert "must_see" not in extract_requirements("不太想去西湖")
    assert "must_see" not in extract_requirements("不一定要去西湖")
    # 正向的强烈意愿不能被否定词误伤
    assert extract_requirements("我太想去西湖了")["must_see"] == ["西湖"]
    print("✓ 否定语境的地点不算必去项")


def test_budget_supports_thousands_separators():
    """预算支持千分位（逗号 / 空格 / 全角逗号），且不跨并列数字拼接。"""
    assert extract_requirements("预算10,000元")["budget"] == 10000
    assert extract_requirements("预算 10 000 元")["budget"] == 10000
    assert extract_requirements("预算1，500元")["budget"] == 1500
    assert extract_requirements("预算5000，住宿另算3000")["budget"] == 5000
    print("✓ 预算支持千分位且不跨并列数字")


def test_fee_number_supports_wan_and_thousands():
    """费用字段的数值支持万单位与千分位。"""
    from utils.itinerary_validation import _number

    assert _number("约1.5万元") == 15000
    assert _number("10,000元") == 10000
    assert _number("1.2万") == 12000
    assert _number(None) is None
    print("✓ 费用数值支持万 / 千分位")


def test_activities_accepts_mapping_and_schedule_key():
    """键→活动字典与 schedule 键的安排同样参与时间冲突检查。"""
    requirements = {"duration_days": 1}
    mapping_plan = {
        "daily_plans": [
            {
                "day": 1,
                "activities": {
                    "morning": {"time": "09:00-11:00", "title": "西湖"},
                    "late": {"time": "10:00-12:00", "title": "灵隐寺"},
                },
            }
        ]
    }
    issues = validate_itinerary(mapping_plan, requirements)
    assert any(issue["code"] == "time_conflict" for issue in issues), issues
    schedule_plan = {
        "daily_plans": [
            {
                "day": 1,
                "schedule": [
                    {"time": "09:00-11:00", "title": "A", "cost": 1000},
                    {"time": "10:00-12:00", "title": "B", "cost": 1000},
                ],
            }
        ]
    }
    issues = validate_itinerary(schedule_plan, requirements)
    assert any(issue["code"] == "time_conflict" for issue in issues), issues
    print("✓ activities 字典 / schedule 键参与校验")


def test_must_see_strips_admin_suffix_and_leading_particles():
    """杭州市 + 西湖 / 杭州市西湖：按行政后缀归一；疑问助词不当地名。"""
    req = extract_requirements(
        "去杭州市出差3天，必去杭州市西湖", {"destination": "杭州市"}
    )
    assert req.get("must_see") == ["西湖"], req
    req = extract_requirements(
        "去杭州玩3天，必去西湖的哪些景点", {"destination": "杭州"}
    )
    assert req.get("must_see") == ["西湖"], req
    # 整座城市不是景点
    assert "must_see" not in extract_requirements(
        "去北京市出差2天，必去北京市", {"destination": "北京市"}
    )
    print("✓ 行政后缀归一与前置助词清洗")


def test_text_duration_skips_ordinals_and_ranges():
    """「第一天」「5日到7日」不是行程总天数。"""
    from utils.itinerary_validation import _text_duration

    assert _text_duration("第一天去西湖，第二天去灵隐寺") is None
    assert _text_duration("5日到7日去杭州") is None
    assert _text_duration("玩3天") == 3
    print("✓ 序数 / 日期区间不算行程天数")


def test_negation_lookalikes_are_positive():
    """「特别 / 分别 / 别人 / 迫不及待」里的别 / 不是否定词。"""
    assert extract_requirements("特别想去西湖")["must_see"] == ["西湖"]
    assert extract_requirements("分别想去西湖和故宫")["must_see"] == ["西湖", "故宫"]
    assert extract_requirements("别人都想去西湖")["must_see"] == ["西湖"]
    assert extract_requirements("我迫不及待想去西湖")["must_see"] == ["西湖"]
    assert extract_requirements("特别别错过西湖")["must_see"] == ["西湖"]
    # 否定契约不能被放宽
    assert "must_see" not in extract_requirements("不想去西湖")
    assert "must_see" not in extract_requirements("不太想去西湖")
    assert "must_see" not in extract_requirements("不一定要去西湖")
    assert extract_requirements("不想去西湖，特别想去故宫")["must_see"] == ["故宫"]
    print("✓ 否定词近似词不再误杀必去项")


def test_budget_prefers_total_and_handles_units():
    """显式预算优先于分项费用；小数 / 千 / k / 中文数字 / 区间按上限。"""
    assert extract_requirements("去杭州玩2天，酒店费用800，预算3000")["budget"] == 3000
    assert (
        extract_requirements("去杭州玩2天，路上花费2小时，预算3000")["budget"]
        == 3000
    )
    assert extract_requirements("预算1.5万元")["budget"] == 15000
    assert extract_requirements("预算0.8万")["budget"] == 8000
    assert extract_requirements("预算3千")["budget"] == 3000
    assert extract_requirements("预算5k")["budget"] == 5000
    assert extract_requirements("预算一万")["budget"] == 10000
    assert extract_requirements("预算三千元")["budget"] == 3000
    assert extract_requirements("预算5000-8000元")["budget"] == 8000
    assert extract_requirements("预算3人分摊5000元")["budget"] == 5000
    assert "budget" not in extract_requirements("花费2小时")
    assert "budget" not in extract_requirements("费用：交通500，住宿600")
    print("✓ 预算：总量优先、单位 / 小数 / 区间")


def test_must_see_suffix_triggers_and_connectors():
    """后置触发词（都要去 / 必须去）与「或」连接词都能提取。"""
    assert extract_requirements("想去西湖或者故宫")["must_see"] == ["西湖", "故宫"]
    assert extract_requirements("去杭州玩3天，西湖、灵隐寺都要去")[
        "must_see"
    ] == ["西湖", "灵隐寺"]
    assert extract_requirements("西湖和灵隐寺一定要去")["must_see"] == [
        "西湖",
        "灵隐寺",
    ]
    assert extract_requirements("故宫必须去")["must_see"] == ["故宫"]
    assert extract_requirements("去杭州玩3天，必去外滩不能错过")[
        "must_see"
    ] == ["外滩"]
    # 后置触发词只在地名与句读之间成立
    assert "must_see" not in extract_requirements("周末都要去爬山")
    assert "must_see" not in extract_requirements("西湖、灵隐寺不一定要去")
    assert "must_see" not in extract_requirements("北京必去景点有哪些")
    # 「不一定都要去」命中的是「都要去」、捕获段以「不一定」结尾：整句是
    # 否定语境，不能产出「西湖灵隐寺不一定」这类假必去项
    assert "must_see" not in extract_requirements("去杭州，西湖灵隐寺不一定都要去")
    assert "must_see" not in extract_requirements("这些景点不一定都要去")
    assert "must_see" not in extract_requirements("去杭州，西湖、灵隐寺不一定都要去")
    # 「到X后」前缀按已知目的地剥离，不把城市与介词收进景点名
    assert extract_requirements("到杭州后西湖、灵隐寺都要去", {"destination": "杭州"})[
        "must_see"
    ] == ["西湖", "灵隐寺"]
    # 守卫只锚定结尾：「大唐不夜城」这类真实地名不能被当成否定
    assert extract_requirements("大唐不夜城都要去")["must_see"] == ["大唐不夜城"]
    print("✓ 后置触发词 /「或」连接词提取")


def test_must_see_cleans_fillers_and_action_suffixes():
    """「顺便去 / 再去 / 也不」与「看夜景」等动作不进入必去项。"""
    assert extract_requirements(
        "我想去北京出差，顺便去故宫", {"destination": "北京"}
    )["must_see"] == ["故宫"]
    assert extract_requirements("想去西湖，再去故宫")["must_see"] == ["西湖", "故宫"]
    assert extract_requirements("必去外滩看夜景")["must_see"] == ["外滩"]
    assert extract_requirements("我想去西湖也不想去灵隐寺")["must_see"] == ["西湖"]
    assert extract_requirements("必须去故宫，别的随意")["must_see"] == ["故宫"]
    print("✓ 必去项清洗分句连接词与动作后缀")


def test_must_see_long_enumeration_not_truncated():
    """超过 24 字的枚举不能截断成半个地名。"""
    requirements = extract_requirements(
        "去杭州玩3天，必去西湖、灵隐寺、雷峰塔、千岛湖、宋城、西溪湿地、河坊街",
        {"destination": "杭州"},
    )
    assert requirements["must_see"] == [
        "西湖",
        "灵隐寺",
        "雷峰塔",
        "千岛湖",
        "宋城",
        "西溪湿地",
        "河坊街",
    ], requirements
    print("✓ 长枚举的必去项完整保留")


def test_duration_fraction_week_and_play_prefix():
    """小数天数向上取整；周单位换算；优先「玩 N 天」。"""
    assert extract_requirements("去杭州出差1.5天")["duration_days"] == 2
    assert extract_requirements("玩一周")["duration_days"] == 7
    assert extract_requirements("玩两周")["duration_days"] == 14
    assert extract_requirements("往返路上2天，在杭州玩3天")["duration_days"] == 3
    assert extract_requirements("", {"duration_days": "7"})["duration_days"] == 7
    assert extract_requirements("", {"duration_days": "7天"})["duration_days"] == 7
    assert "duration_days" not in extract_requirements("", {"duration_days": True})
    assert "duration_days" not in extract_requirements("周末")
    print("✓ 小数 / 周 / 字符串天数")


def test_must_see_checks_activity_identity_only():
    """主题 / 备注 / 描述里提到的地名不算「已安排」。"""
    plan = {
        "daily_plans": [
            {
                "day": 1,
                "theme": "西湖一日游",
                "notes": "因闭馆未去灵隐寺",
                "activities": [],
            }
        ]
    }
    codes = {
        issue["code"]
        for issue in validate_itinerary(plan, {"must_see": ["西湖", "灵隐寺"]})
    }
    assert codes == {"must_see_missing"}, codes
    plan_ok = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-11:00", "location": "西湖", "description": "游览"}
                ],
            }
        ]
    }
    assert validate_itinerary(plan_ok, {"must_see": ["西湖"]}) == []
    plan_desc = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {
                        "time": "09:00-11:00",
                        "location": "某公园",
                        "description": "如时间充裕可去雷峰塔",
                    }
                ],
            }
        ]
    }
    assert any(
        issue["code"] == "must_see_missing"
        for issue in validate_itinerary(plan_desc, {"must_see": ["雷峰塔"]})
    )
    print("✓ 必去项只核对活动身份字段")


def test_time_range_cross_midnight_and_dashes():
    """跨午夜时间合法；次日 / 24:00 / 全角连接符可解析。"""
    from utils.itinerary_validation import _parse_time_range

    assert _parse_time_range("22:00-02:00") == (1320, 120)
    assert _parse_time_range("14:00-次日02:00") == (840, 1560)
    assert _parse_time_range("22:00-24:00") == (1320, 1440)
    assert _parse_time_range("09:00–12:00") == (540, 720)
    assert _parse_time_range("09:00～12:00") == (540, 720)
    plan = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "22:00-02:00", "title": "夜班火车"},
                    {"time": "09:00-10:00", "title": "早餐"},
                ],
            }
        ]
    }
    assert validate_itinerary(plan, {}) == []
    bad = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [{"time": "15:00-09:00", "title": "夜游"}],
            }
        ]
    }
    assert any(
        issue["code"] == "invalid_time_range"
        for issue in validate_itinerary(bad, {})
    )
    overlap = {
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "20:00-次日01:00", "title": "甲"},
                    {"time": "23:00-次日00:30", "title": "乙"},
                ],
            }
        ]
    }
    assert any(
        issue["code"] == "time_conflict"
        for issue in validate_itinerary(overlap, {})
    )
    print("✓ 跨午夜时间区间与连接符")


def test_number_prefers_amount_over_quantity():
    """「2人共500元」取金额而非人数；区间取上限。"""
    from utils.itinerary_validation import _number

    assert _number("2人共500元") == 500
    assert _number("门票2张共120元") == 120
    assert _number("人均300元共900元") == 900
    assert _number("约3000-5000元") == 5000
    assert _number("约1.5万元") == 15000
    assert _number("-5") == -5
    plan = {
        "estimated_budget": 500,
        "daily_plans": [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-10:00", "title": "A", "cost": "2人共500元"}
                ],
            }
        ],
    }
    assert validate_itinerary(plan, {}) == []
    print("✓ 费用数字优先取带单位的金额")


def test_cn_number_place_value_semantics():
    """中文金额按位值解析：两千五=2500、一万五=15000、三千零五十=3050。"""
    from utils.itinerary_validation import _cn_number

    assert _cn_number("两千五") == 2500
    assert _cn_number("一千五") == 1500
    assert _cn_number("一万五") == 15000
    assert _cn_number("三千零五十") == 3050
    assert _cn_number("一百零五") == 105
    assert _cn_number("一万零五") == 10005
    assert _cn_number("十二") == 12
    assert _cn_number("一百二十三") == 123
    assert _cn_number("三千") == 3000
    assert _cn_number("一万") == 10000
    assert extract_requirements("去杭州出差3天，预算两千五")["budget"] == 2500
    assert extract_requirements("预算一万五千元")["budget"] == 15000
    assert extract_requirements("预算三千零五十元")["budget"] == 3050
    print("✓ 中文金额位值解析")


def test_itemized_cost_is_not_total_budget():
    """机票 / 酒店等分项花费不能被当成整趟预算上限。"""
    assert "budget" not in extract_requirements("机票花费2000，帮我规划3天杭州行程")
    assert "budget" not in extract_requirements("酒店花费800，预算还没定")
    assert "budget" not in extract_requirements("住宿费用800")
    assert "budget" not in extract_requirements("门票费用600")
    assert "budget" not in extract_requirements("人均花费800")
    assert extract_requirements("总花费5000，机票2000")["budget"] == 5000
    assert extract_requirements("全程费用5000")["budget"] == 5000
    # 显式预算仍优先于任何分项
    assert extract_requirements("酒店费用800，预算3000")["budget"] == 3000
    print("✓ 分项花费不当作总预算")


def test_activity_phrases_are_not_must_see():
    """「想去爬山 / 泡温泉 / 吃火锅」等动作诉求不是必去景点。"""
    for query in (
        "周末想去爬山",
        "想去泡温泉",
        "想去逛街",
        "想去滑雪",
        "想去看升旗",
        "想去吃火锅",
        "想去听演唱会",
    ):
        assert "must_see" not in extract_requirements(query), query
    # 真实地名仍保留，动作后缀从地名剥离
    assert extract_requirements("想去西湖")["must_see"] == ["西湖"]
    assert extract_requirements("想去颐和园爬山")["must_see"] == ["颐和园"]
    assert extract_requirements("必去外滩看夜景")["must_see"] == ["外滩"]
    print("✓ 动作诉求不进入必去项")


def test_long_must_see_enumeration_beyond_200_chars():
    """超过 200 字符的必去项枚举不能被静默截断。"""
    names = [f"甲{i:02d}园林" for i in range(1, 50)]
    query = "去北京玩5天，必去" + "、".join(names)
    assert len("、".join(names)) > 200
    assert extract_requirements(query)["must_see"] == names
    print("✓ 超长必去项枚举完整保留")


def test_extract_requirements_handles_numeric_overflow():
    """超长数字 / 中文数字串不能让约束提取抛异常。"""
    assert extract_requirements("玩" + "9" * 400 + ".5天") == {}
    assert extract_requirements("玩" + "十" * 2000 + "天") == {}
    assert extract_requirements("玩" + "9" * 5000 + "天") == {}
    # 正向回归：合法数字不受长度上限误杀
    assert extract_requirements("玩1.5天")["duration_days"] == 2
    assert extract_requirements("玩三天")["duration_days"] == 3
    assert extract_requirements("预算一万")["budget"] == 10000
    print("✓ 超长数字串不抛异常")


def test_out_of_range_time_reported_invalid():
    """25:00 / 10:99 等越界时刻要报 invalid_time_range，不能静默跳过。"""
    plan = {
        "daily_plans": [
            {
                "activities": [
                    {"time": "25:00-26:00", "title": "越界A"},
                    {"time": "10:99-11:99", "title": "越界B"},
                ]
            }
        ]
    }
    codes = [issue["code"] for issue in validate_itinerary(plan, {})]
    assert codes.count("invalid_time_range") == 2, codes
    # 22:00-24:00 仍是合法写法；无 time / 未支持写法不报
    assert validate_itinerary(
        {"daily_plans": [{"activities": [{"time": "22:00-24:00"}]}]}, {}
    ) == []
    assert validate_itinerary(
        {"daily_plans": [{"activities": [{"title": "上午自由活动"}]}]}, {}
    ) == []
    print("✓ 越界时刻报 invalid_time_range")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_extract_requirements_budget_must_see_duration()
    test_extract_requirements_wan_and_chinese_duration()
    test_date_day_is_not_duration_and_dates_drive_day_count()
    test_budget_does_not_cross_clause_punctuation()
    test_must_see_trims_duration_and_budget_fragments()
    test_must_see_skips_narrative_and_preserves_connector_names()
    test_must_see_strips_destination_and_purpose()
    test_budget_does_not_cross_enumeration_punctuation()
    test_must_see_only_checks_daily_plans()
    test_validate_passes_for_consistent_plan()
    test_validate_detects_all_five_checks()
    test_validate_handles_empty_and_unknown_shapes()
    test_must_see_enumeration_with_separators()
    test_must_see_ignores_negated_places()
    test_budget_supports_thousands_separators()
    test_fee_number_supports_wan_and_thousands()
    test_activities_accepts_mapping_and_schedule_key()
    test_must_see_strips_admin_suffix_and_leading_particles()
    test_text_duration_skips_ordinals_and_ranges()
    test_negation_lookalikes_are_positive()
    test_budget_prefers_total_and_handles_units()
    test_must_see_suffix_triggers_and_connectors()
    test_must_see_cleans_fillers_and_action_suffixes()
    test_must_see_long_enumeration_not_truncated()
    test_duration_fraction_week_and_play_prefix()
    test_must_see_checks_activity_identity_only()
    test_time_range_cross_midnight_and_dashes()
    test_number_prefers_amount_over_quantity()
    test_cn_number_place_value_semantics()
    test_itemized_cost_is_not_total_budget()
    test_activity_phrases_are_not_must_see()
    test_long_must_see_enumeration_beyond_200_chars()
    test_extract_requirements_handles_numeric_overflow()
    test_out_of_range_time_reported_invalid()
    print("\n全部通过")
