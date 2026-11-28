"""行程约束校验（v2.0）。

职责：
1. 从用户需求 / 事项收集结果里提取可校验的约束：天数、预算、必去项；
2. 对 LLM 生成的行程做五项检查：天数、时间冲突、预算、必去项、费用复核；
3. 输出结构化问题列表，供编排层展示或带原因重新生成。

只做确定性校验，不调用模型。
"""

from __future__ import annotations

import math
import re
from datetime import date as _date
from typing import Any, Iterable

from utils.entity_normalize import normalize_event_data

_MUST_SEE_TRIGGERS = (
    "必去|一定要去|必须去|想去|打卡|别错过|不能错过|非去不可"
)
_BUDGET_RE = re.compile(
    r"(?P<kind>预算|总预算|花费|费用)"
    # 冒号后必须紧跟金额（「费用：交通500」是分项不是总预算）；
    # 无冒号时允许「预算大约是」等短填充，但不能跨越顿号 / 逗号 / 句号；
    # 「预算3人分摊5000元」里的数量词是填充，不是总预算
    r"(?:[：:]\s*"
    r"|\d+\s*(?:人|位)(?:共|均|分摊|平摊|左右)?[^\d。！？；;，,、：:\n]{0,4}"
    r"|[^\d。！？；;，,、：:\n]{0,8}?)"
    # 金额支持千分位与小数、中文数字（一万 / 三千 / 五千元）
    r"(?P<amount>\d{1,3}(?:[,， ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|[一二两三四五六七八九十百千万零]+)"
    r"\s*(?P<unit>万元|万|千元|千|[kK](?![A-Za-z])|[wW](?![A-Za-z])|元|块)?"
    # 区间「5000-8000 元」按上限理解（用户可接受到更高一档）
    r"(?:\s*[-~—–至到]\s*"
    r"(?P<amount2>\d{1,3}(?:[,， ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|[一二两三四五六七八九十百千万零]+)"
    r"\s*(?P<unit2>万元|万|千元|千|元|块)?"
    r")?"
    # 「花费2小时」「预算3天」不是金额
    r"(?!\s*(?:个?(?:半)?(?:小时|钟头)|分钟|天|日|周|月))"
)
# 分项开销不是整趟预算：被这些名词修饰的「花费 / 费用」只参与分项费用
# 复核，不能当作总预算上限——否则正常行程会被误判 budget_exceeded 并触发
# 带错误原因的重生成
_ITEM_COST_PREFIXES = (
    "机票", "车票", "火车", "高铁", "航班", "酒店", "住宿", "宾馆", "民宿",
    "交通", "打车", "出租", "地铁", "公交", "租车", "油费", "燃油",
    "门票", "餐饮", "伙食", "餐费", "吃饭", "人均", "每人",
)
_MUST_SEE_RE = re.compile(
    r"(?:" + _MUST_SEE_TRIGGERS + r")[：:\s]*"
    # 捕获串允许顿号 / 逗号分隔符（交给 _split_must_see 拆分），但惰性
    # 截到下一个触发词 / 句末为止——不能让一个触发词的捕获吞掉下一句
    # 触发词（否则否定语境会连带把后面的正常必去项一起丢掉）。分句连接
    # 词（也不 / 还 / 再…）也作为边界，避免把「西湖也不」这类残句带进
    # 必去项。没有边界的长列表退回分隔符截断，绝不保存半个名字
    r"("
    r"[一-鿿A-Za-z0-9·、，,；;/ ]{2,200}?"
    r"(?=(?:也不|也都|还|也|就|再|顺便|然后|接着)?"
    r"(?:" + _MUST_SEE_TRIGGERS + r")|[。！？；;\n]|$)"
    r"|"
    r"[一-鿿A-Za-z0-9·、，,；;/ ]{2,24}(?=[、，,；;/ ]|$)"
    r")"
)
# 24 字兜底在枚举分隔符处截断后，继续取同一枚举的剩余项到句末 / 下一个
# 触发词，避免超过 200 字符的必去项列表被静默丢弃
_MUST_SEE_ENUM_TAIL_RE = re.compile(
    r"[一-鿿A-Za-z0-9·、，,；;/ ]{2,}?"
    r"(?=(?:也不|也都|还|也|就|再|顺便|然后|接着)?"
    r"(?:" + _MUST_SEE_TRIGGERS + r")|[。！？；;\n]|$)"
)
# 后置触发词：「西湖、灵隐寺都要去 / 故宫必须去 / 外滩不能错过」
_MUST_SEE_SUFFIX_RE = re.compile(
    r"(?:^|[。！？；;\n，,])\s*([一-鿿A-Za-z0-9·、；;/ ]{2,48}?)"
    r"\s*(?:必须都要去|都必须去|必须去|都要去|都得去|都去|一定要去|不能错过|非去不可)"
    r"(?=[。！？；;\n，,]|$)"
)
# 否定语境（不想去 / 不太想去 / 不一定要去 / 不必去 / 无需去）：触发词
# 前 6 个字里出现否定词、且否定词与触发词之间最多隔 2 个字时不算必去项。
# 「别」须是独立否定词（特别 / 分别 / 别人 / 个别 / 级别 / 告别 不是），
# 「不」前面是「迫 / 得」时是肯定（迫不及待 / 不得不）
_MUST_SEE_NEGATION_RE = re.compile(
    r"(?:(?<![迫得])不|无需|无须|不用|不要|不必|未必"
    r"|(?<![特分个告级类性别派区识])别(?!人|的|处))"
    r"[^，。！？；\n]{0,2}$"
)
# 费用字段的数值：千分位组（逗号后恰好三位）整体匹配，支持万 / 千单位
_NUMBER_RE = re.compile(
    r"((?<![\d.])-?(?:\d{1,3}(?:[,， ]\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(万元|万|千元|千|[kK](?![A-Za-z])|元|块)?"
)
# 城市名的行政后缀：目的地「杭州市」与必去项「杭州市西湖」按「杭州」比较
_ADMIN_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "市", "省", "区", "县")
# 「3月4日」里的「4日」是日期不是行程天数：匹配后再看数字前是否紧邻「月」
# 小数天数（1.5 天）整体捕获，不能只取小数点后的尾数
_DURATION_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(?:天|日)"
)
# 「一周 / 两周 / 3 个星期」：周单位换算成 7 天
_WEEK_DURATION_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(?:周|个?星期|个?礼拜)"
)
# 必去项里混入的天数 / 预算片段：截断后再清洗动词后缀
_MUST_SEE_DURATION_RE = re.compile(
    r"(?:\d+(?:\.\d+)?|[0-9一二两三四五六七八九十]+)\s*[天日].*$"
)
_MUST_SEE_BUDGET_RE = re.compile(r"(?:预算|花费|费用|人均|总价|左右|以内).*$")
# 「和 / 与 / 及 / 或」既可能是并列连接词，也可能是地名的一部分（颐和园、
# 和平饭店）：只在两侧都像完整名称时才拆
_MUST_SEE_CONNECTOR_RE = re.compile(r"(?:或者|或|和|与|及)")
_MUST_SEE_GENERIC = frozenset(
    {
        "景点",
        "景区",
        "地方",
        "城市",
        "路线",
        "行程",
        "酒店",
        "美食",
        "旅游",
        "旅行",
        "好玩",
        # 后置触发词的捕获段常见主语 / 时间词，不是地名
        "我们",
        "你们",
        "他们",
        "她们",
        "大家",
        "咱们",
        "自己",
        "这次",
        "下次",
        "到时候",
        "今天",
        "明天",
        "后天",
        "周末",
        "假期",
        # 「必须去故宫，别的随意」：收尾短语不是地名
        "随意",
        "都行",
        "都可以",
        "也行",
        "就好",
        "即可",
        "就行",
        "别的",
        "其他",
        "其它",
    }
)
_TIME_RANGE_RE = re.compile(
    # 连接符兼容半角 / 全角 / en dash；end 前可带「次日 / 翌日」跨午夜标记
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:-|–|~|～|—|－|至|到)\s*"
    r"(?:(次日|翌日|第二天)\s*)?(\d{1,2})\s*[:：]\s*(\d{2})"
)
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNIT_VALUES = {"万": 10000, "千": 1000, "百": 100, "十": 10}
# 超长数字串不是天数或预算：float 溢出成 inf（math.ceil 抛 OverflowError）、
# int 在 >4300 位抛 ValueError、中文数字递归过深会 RecursionError
_MAX_NUMBER_TEXT = 32


def _cn_number(value: str) -> int | None:
    """中文数字转整数（十 / 百 / 千 / 万 组合；「一百零五」「三千」「两千五」）。

    ``两千五`` 按位值读作 2500（单位后的尾随数字取下一级位权），而不是
    2005；``一万五`` 读作 15000。没有单位的纯数字串（``三五``）保持旧的
    逐字拼接语义。
    """
    text = str(value or "").strip()
    # 超长数字串（粘贴的编号 / 连击数字）不是天数或预算：int() 在 >4300
    # 位时抛 ValueError，中文数字的递归解析也没有深度上限（RecursionError）
    if not text or len(text) > _MAX_NUMBER_TEXT:
        return None
    if text.isdigit():
        return int(text)
    if not any(char in _CN_UNIT_VALUES for char in text):
        digits = [_CN_DIGITS.get(char) for char in text]
        if all(number is not None for number in digits):
            return int("".join(str(number) for number in digits))
        return None
    total = section = number = 0
    pending = 0
    zero_seen = False
    for char in text:
        if char == "零":
            zero_seen = True
            continue
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            continue
        unit = _CN_UNIT_VALUES.get(char)
        if unit is None:
            return None
        if unit == 10000:
            total += (section + number) * unit
            section = 0
        else:
            section += (number or 1) * unit
        pending, number, zero_seen = unit, 0, False
    if number:
        # 单位后的尾随数字取下一级位权：两千五→500、一万五→5000；
        # 「三千零五十」的零之后是实位，按个位累加
        section += number if zero_seen else number * max(1, pending // 10)
    return total + section


def _strip_admin_suffix(value: str) -> str:
    """去掉城市名末尾的行政后缀（杭州市 → 杭州），太短的保持原样。"""
    text = str(value or "").strip()
    for suffix in _ADMIN_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            return text[: -len(suffix)]
    return text


def _strip_leading_admin_suffix(value: str) -> str:
    """去掉「市 / 省」等被前缀剥离后残留在开头的一个行政后缀字。"""
    text = str(value or "").strip()
    for suffix in ("市", "省", "区", "县"):
        if text.startswith(suffix) and len(text) > len(suffix) + 1:
            return text[len(suffix) :].strip()
    return text


def _split_connected(part: str) -> list[str]:
    """递归按连接词拆分：取第一个两侧都 ≥2 字的连接点。"""
    connectors = [
        match
        for match in _MUST_SEE_CONNECTOR_RE.finditer(part)
        if 0 < match.start() < len(part) - 1
    ]
    for match in connectors:
        left = part[: match.start()].strip()
        right = part[match.end() :].strip()
        if len(left) >= 2 and len(right) >= 2:
            return [left] + _split_connected(right)
    return [part]


def _split_must_see(value: str) -> list[str]:
    """按顿号 / 逗号与「和 / 与 / 及」拆分必去项，保护地名里的连接字。

    例：「西湖和灵隐寺」→ 西湖 / 灵隐寺；「颐和园」「和平饭店」保持完整
    （连接字两侧不是两个完整名称时不拆）；「颐和园和圆明园」能跳过
    园名里的「和」在后面的连接点拆分。
    """
    parts = [
        part.strip()
        for part in re.split(
            r"[、，,；;/\s]+",
            # 千分位逗号不是枚举分隔符（10,000 不能被拆成「10」「000元」）
            re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", str(value or "")),
        )
        if part.strip()
    ]
    names: list[str] = []
    for part in parts:
        names.extend(_split_connected(part))
    return names


def _clean_must_see(value: str) -> list[str]:
    """把「西湖和灵隐寺」拆成多个必去项，并去掉尾随动词 / 助词。"""
    parts = _split_must_see(value)
    trailing = (
        "逛逛",
        "看看",
        "玩一玩",
        "玩",
        "打卡",
        "参观",
        "游览",
        "旅游",
        "旅行",
        "游玩",
        "出差",
        "好玩",
        "有哪些",
        "有什么",
        "哪些",
        "什么",
        "哪个",
        "哪里",
        "地点",
        "地方",
        "景点",
        "景区",
        "转转",
        "游",
        "的",
        "了",
        "一下",
        # 「西湖也不想去灵隐寺」的残句 / 「别的随意」的收尾
        "也不",
        "也都",
        "也",
        "随意",
        "都行",
        "都可以",
        "也行",
        "就好",
        "即可",
        "就行",
        # 「必去外滩看夜景」：动作不是地名的一部分
        "看夜景",
        "看升旗",
        "看日出",
        "看日落",
        "看表演",
        "看演出",
        "看灯光秀",
        "看展览",
        "吃火锅",
        "吃小吃",
        "吃美食",
        # 纯活动诉求（想去爬山 / 泡温泉 / 逛街 / 滑雪 / 听演唱会）不是
        # 具体景点：作为尾随后缀剥离，整词命中时剥成空后丢弃
        "爬山",
        "登山",
        "徒步",
        "露营",
        "滑雪",
        "泡温泉",
        "逛街",
        "购物",
        "听演唱会",
        "看演唱会",
    )
    names: list[str] = []
    for part in parts:
        name = part.strip().strip("。.!！?？ ")
        # 触发词后面的前置助词 / 疑问词不是地名的一部分（「西湖的哪些景点」）
        name = re.sub(
            r"^(?:的|了|哪些|什么|哪个|哪儿|哪里|地点|地方|景点|还有|大概|大约)+",
            "",
            name,
        ).strip()
        # 分句连接词 / 副词起头（「顺便去故宫」「再去灵隐寺」「别的随意」）；
        # 单剥「还 / 再 / 也 / 就」会破坏「还剑湖」这类真实地名，必须带动作词
        name = re.sub(
            r"^(?:(?:顺便|然后|接着|之后|另外|并且|还有)+(?:想|要|去|到|来|逛|看|玩|参观|游览|打卡)?"
            r"|(?:再|也|就|还)+(?:想|要|去|到|来|逛|看|玩|参观|游览|打卡)+"
            r"|(?:必去|一定要去|必须去|想去|打卡|别错过|不能错过|非去不可)+"
            r"|(?:别的|其它的|其他|剩下的)(?:地方|景点|行程|安排)?)",
            "",
            name,
        ).strip()
        # 「西湖玩3天」「故宫预算5000」：先截掉混入的天数 / 预算片段
        name = _MUST_SEE_DURATION_RE.sub("", name)
        name = _MUST_SEE_BUDGET_RE.sub("", name)
        changed = True
        while changed and name:
            changed = False
            for suffix in trailing:
                # 整词恰等于动作后缀（想去爬山 / 吃火锅 / 看升旗）时也剥离，
                # 剥成空后由下面的 not name 丢弃；真实地名+动作仍保留地名
                if name.endswith(suffix):
                    name = name[: -len(suffix)].strip()
                    changed = True
        if not name:
            continue
        if name.isdigit() or name in _MUST_SEE_GENERIC:
            continue
        # 拆分千分位后残留的纯金额片段（「000元」）不是地名
        if re.fullmatch(r"[\d,，.\s]*元?", name):
            continue
        if 2 <= len(name) <= 24 and name not in names:
            names.append(name)
    return names


def _iso_date(value: object) -> _date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    # 末尾紧跟数字（2026-11-100）是写错位的日期：盲截前 10 位会把「100 日」
    # 静默算成 10 号，行程天数 / 冲突检测随之用错日期。
    if len(text) > 10 and text[10].isdigit():
        return None
    try:
        return _date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_range_days(requirements: dict[str, Any]) -> int | None:
    """起止日期都在时用日期算天数（比模型抽取的 duration_days 可靠）。"""
    start = _iso_date(requirements.get("start_date"))
    end = _iso_date(requirements.get("end_date"))
    if start is None or end is None or end < start:
        return None
    days = (end - start).days + 1
    return days if 0 < days <= 366 else None


def _duration_number(raw: str) -> int | None:
    """天数数值：小数天数向上取整（1.5 天跨两个日历日）。"""
    text = str(raw or "").strip()
    if len(text) > _MAX_NUMBER_TEXT:
        return None
    if "." in text:
        try:
            return math.ceil(float(text))
        except (ValueError, OverflowError):
            return None
    return _cn_number(text)


def _text_duration(text: str) -> int | None:
    """「N 天 / 日」「N 周」天数；跳过「3月4日」「第一天」「5日到7日」。

    序数（第 N 天）与日期区间（N 日到 M 日）不是行程总天数：把「第一天」
    读成 1 天会让天数校验把正确行程判失败。优先「玩 / 待 / 住 / 停留」
    后面的天数：「往返路上 2 天，在杭州玩 3 天」取 3。
    """
    preferred: int | None = None
    fallback: int | None = None
    for match in _DURATION_RE.finditer(text):
        prefix = text[: match.start()].rstrip()
        if prefix.endswith(("月", "第", "到", "至", "~", "—", "-")):
            continue
        suffix = text[match.end() :].lstrip()
        if suffix[:1] in ("到", "至", "~", "—", "-"):
            continue
        number = _duration_number(match.group(1))
        if not number or not 0 < number <= 365:
            continue
        if prefix.endswith(("玩", "待", "住", "停留", "游玩", "旅游", "行程")):
            preferred = number
            break
        if fallback is None:
            fallback = number
    if preferred is not None:
        return preferred
    if fallback is not None:
        return fallback
    for match in _WEEK_DURATION_RE.finditer(text):
        prefix = text[: match.start()].rstrip()
        if prefix.endswith(("第", "到", "至", "~", "—", "-")):
            continue
        suffix = text[match.end() :].lstrip()
        # 「三周年」是年份不是行程长度
        if suffix[:1] in ("到", "至", "~", "—", "-", "年"):
            continue
        number = _duration_number(match.group(1))
        if number and 0 < number * 7 <= 365:
            return number * 7
    return None


def _budget_amount(raw: object, unit: object) -> float | None:
    """预算金额归一：千分位 / 小数 / 中文数字，万、千、k、w 单位换算。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text[0].isdigit():
        try:
            amount = float(re.sub(r"[,，\s]", "", text))
        except ValueError:
            return None
    else:
        number = _cn_number(text)
        if number is None:
            return None
        amount = float(number)
    unit_text = str(unit or "")
    if unit_text in ("万元", "万", "w", "W"):
        amount *= 10000
    elif unit_text in ("千元", "千", "k", "K"):
        amount *= 1000
    return amount if math.isfinite(amount) else None


def extract_requirements(
    user_query: object, event_data: object = None
) -> dict[str, Any]:
    """从用户原话与事项收集结果提取可校验约束（缺失的键不返回）。"""
    requirements: dict[str, Any] = {}
    event = normalize_event_data(event_data)
    duration = event.get("duration_days")
    if isinstance(duration, str):
        # 模型 / 备份里的字符串天数（"7" / "7天"）此前被静默丢弃
        stripped = duration.strip()
        if re.fullmatch(r"\d{1,3}", stripped):
            duration = int(stripped)
        else:
            duration = _text_duration(stripped)
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and 0 < int(duration) <= 366
    ):
        requirements["duration_days"] = int(duration)
    for key in ("start_date", "end_date"):
        value = event.get(key)
        # 只接受能被解析成 ISO 日期的值：归一失败的原串不能当合法需求
        if isinstance(value, str) and _iso_date(value) is not None:
            requirements[key] = value.strip()
    # 起止日期完整时以日期算出的天数为准：模型抽取的 duration_days 与
    # 日期冲突时（或根本没抽）会漏掉天数校验
    date_days = _date_range_days(requirements)
    if date_days is not None:
        requirements["duration_days"] = date_days

    text = str(user_query or "")
    # 「酒店费用800，预算3000」：显式的 预算 / 总预算 优先；没有时再退化
    # 到第一个 费用 / 花费 候选（时间 / 数量数字已由正则排除）
    budget_match = None
    fallback_match = None
    for candidate in _BUDGET_RE.finditer(text):
        if candidate.group("kind") in ("预算", "总预算"):
            budget_match = candidate
            break
        if fallback_match is not None:
            continue
        # 「酒店费用800 / 机票花费2000」是分项开销；只有「总花费 / 全程费用 /
        # 裸费用」这类整体表述才退化作为总预算
        prefix = text[max(0, candidate.start() - 4) : candidate.start()]
        if prefix.endswith(_ITEM_COST_PREFIXES):
            continue
        fallback_match = candidate
    budget_match = budget_match or fallback_match
    if budget_match:
        amounts = [
            _budget_amount(budget_match.group("amount"), budget_match.group("unit"))
        ]
        if budget_match.group("amount2"):
            amounts.append(
                _budget_amount(
                    budget_match.group("amount2"), budget_match.group("unit2")
                )
            )
        amount = max((value for value in amounts if value is not None), default=None)
        if amount is not None and amount > 0:
            requirements["budget"] = amount

    must_see: list[str] = []
    pos = 0
    while True:
        match = _MUST_SEE_RE.search(text, pos)
        if match is None:
            break
        pos = match.end()
        # 「不想去 / 不太想去 / 不一定要去 / 不必去」里的地点不是必去项
        negated = text[max(0, match.start() - 6) : match.start()]
        if _MUST_SEE_NEGATION_RE.search(negated):
            continue
        captured = match.group(1)
        # 24 字兜底在分隔符处截断（第一分支的边界不含分隔符，停在分隔符
        # 只可能是兜底分支）：继续取同一枚举的剩余项，不能静默丢后面的必去项
        while pos < len(text) and text[pos] in "、，,/ ":
            rest = _MUST_SEE_ENUM_TAIL_RE.match(text, pos)
            if rest is None:
                break
            captured += rest.group(0)
            pos = rest.end()
        for name in _clean_must_see(captured):
            if name not in must_see:
                must_see.append(name)
    # 后置触发词：「西湖、灵隐寺都要去 / 故宫必须去」；「不一定要去」里
    # 触发词前紧邻「不」时是否定，不能算必去项；「不一定都要去」命中的
    # 是「都要去」、捕获段以「不一定 / 未必 / 不都」结尾，同一守卫要认
    for match in _MUST_SEE_SUFFIX_RE.finditer(text):
        raw = match.group(1).rstrip()
        if re.search(r"(?:不一定|未必|并非|不都|不全都?)$|[不别没未]$", raw):
            continue
        for name in _clean_must_see(raw):
            if name not in must_see:
                must_see.append(name)
    destination = str(event.get("destination") or "").strip()
    if must_see and destination:
        dest_core = _strip_admin_suffix(destination)
        # 只对已知目的地生效：「到杭州后西湖」剥成「西湖」；destination
        # 为空时不动，避免误剥「到后海」这类真实地名
        forms = sorted(
            {
                form
                for base in (destination, dest_core)
                if base
                for form in (base, f"到{base}后", f"到{base}", f"去{base}后")
            },
            key=len,
            reverse=True,
        )
        refined: list[str] = []
        for name in must_see:
            name_core = _strip_admin_suffix(name)
            # 「北京出差」这类只剩城市名的，是出行目的不是景点
            if name == destination or name_core == dest_core:
                continue
            # 「杭州市西湖」「杭州西湖」都剥掉城市前缀后保留「西湖」
            stripped = name
            for form in forms:
                if stripped.startswith(form) and len(stripped) >= len(form) + 2:
                    stripped = _strip_leading_admin_suffix(
                        stripped[len(form) :].strip()
                    )
                    break
            name = stripped
            if name and name not in _MUST_SEE_GENERIC and name not in refined:
                refined.append(name)
        must_see = refined
    if must_see:
        requirements["must_see"] = must_see

    if "duration_days" not in requirements:
        duration_days = _text_duration(text)
        if duration_days:
            requirements["duration_days"] = duration_days
    return requirements


def _daily_plans(itinerary: object) -> list[dict]:
    if not isinstance(itinerary, dict):
        return []
    plans = itinerary.get("daily_plans")
    if isinstance(plans, list):
        return [plan for plan in plans if isinstance(plan, dict)]
    if isinstance(plans, dict):
        ordered: list[dict] = []
        for key in sorted(plans, key=lambda item: str(item)):
            value = plans[key]
            if isinstance(value, dict):
                ordered.append(value)
        return ordered
    return []


def _activities(plan: dict) -> list[dict]:
    """一天的活动列表：支持列表、「键→活动」字典与 schedule 等别名键。

    LLM 输出偶尔把 activities 写成 ``{"09:00": {...}, "14:00": {...}}``，
    或把安排放在 schedule 键下——只认 list / 单个 dict 会让时间冲突与
    费用检查整体静默跳过。
    """
    for key in ("activities", "schedule", "安排", "计划"):
        items = plan.get(key)
        if isinstance(items, list):
            activities = [item for item in items if isinstance(item, dict)]
            if activities:
                return activities
        elif isinstance(items, dict):
            if any(
                field in items
                for field in ("time", "title", "name", "activity", "cost", "price", "fee")
            ):
                return [items]
            nested = [
                value for value in items.values() if isinstance(value, dict)
            ]
            return nested or [items]
    return []


def _parse_time_range(value: object) -> tuple[int, int] | None:
    match = _TIME_RANGE_RE.search(str(value or ""))
    if not match:
        return None
    hour, minute, next_day, end_hour, end_minute = match.groups()
    hour, minute = int(hour), int(minute)
    end_hour, end_minute = int(end_hour), int(end_minute)
    if hour > 23 or minute > 59 or end_minute > 59:
        return None
    # 「22:00-24:00」：24:00 是当天结束的合法写法
    if end_hour > 24 or (end_hour == 24 and end_minute != 0):
        return None
    start = hour * 60 + minute
    end = end_hour * 60 + end_minute
    if next_day:
        end += 1440
    return start, end


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        # 费用字段可能是「约1.5万元」「10,000元」「2人共500元」：支持
        # 千分位与万 / 千单位；数量词（2 人 / 2 张）不是金额，优先取
        # 「共 / 合计 / 总价」修饰或带货币单位的数
        matches = list(_NUMBER_RE.finditer(value))
        if not matches:
            return None
        chosen = None
        total_hit = None
        for match in matches:
            if match.group(2) not in ("元", "块", "万元", "万", "千元", "千"):
                continue
            prefix = value[max(0, match.start() - 4) : match.start()]
            if re.search(r"(?:共|合计|总计|共计|总价)", prefix):
                total_hit = match
                break
            if chosen is None:
                chosen = match
        match = total_hit or chosen or matches[-1]
        raw = re.sub(r"[,，\s]", "", match.group(1))
        try:
            number = float(raw)
        except ValueError:
            return None
        if match.group(2) in ("万元", "万"):
            number *= 10000
        elif match.group(2) in ("千元", "千"):
            number *= 1000
    else:
        return None
    return number if math.isfinite(number) else None


def _activity_cost(activity: dict) -> float | None:
    for key in ("cost", "price", "fee", "estimated_cost", "花费", "费用"):
        if key in activity:
            number = _number(activity.get(key))
            if number is not None:
                return number
    return None


def _estimate(itinerary: dict) -> float | None:
    for key in ("estimated_budget", "total_cost", "budget", "总预算"):
        if key in itinerary:
            number = _number(itinerary.get(key))
            if number is not None:
                return number
    return None


def validate_itinerary(
    itinerary: object, requirements: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """五项检查；返回 ``[{code, message}, ...]``，空列表表示通过。"""
    requirements = requirements or {}
    issues: list[dict[str, str]] = []
    if not isinstance(itinerary, dict):
        return [
            {
                "code": "missing_itinerary",
                "message": "行程结果不是对象，无法校验",
            }
        ]
    plans = _daily_plans(itinerary)
    if not plans:
        return [
            {
                "code": "missing_daily_plans",
                "message": "行程没有每日安排（daily_plans 为空）",
            }
        ]

    # 1) 天数
    expected_days = requirements.get("duration_days")
    if isinstance(expected_days, int) and expected_days > 0 and len(plans) != expected_days:
        issues.append(
            {
                "code": "day_count_mismatch",
                "message": (
                    f"要求 {expected_days} 天，实际安排了 {len(plans)} 天"
                ),
            }
        )

    # 2) 时间冲突 / 非法时间
    # carry: 前一天跨午夜活动落到当天时间轴上的尾巴（绝对分钟已减 1440）。
    # 反向区间最多 12 小时，尾巴不会越过次日全天，一步传递足够。
    carry: list[tuple[int, str, int]] = []
    for day_index, plan in enumerate(plans, 1):
        intervals: list[tuple[int, int, str]] = []
        for activity in _activities(plan):
            raw_time = activity.get("time")
            parsed = _parse_time_range(raw_time)
            label = str(
                activity.get("title")
                or activity.get("name")
                or activity.get("activity")
                or raw_time
            )
            if parsed is None:
                # 形态是「HH:MM-HH:MM」却解析失败 = 时/分越界（25:00 /
                # 10:99）；与「没写时间 / 未支持写法」不同，不能静默跳过，
                # 否则非法时间既不报错也不参与重叠检测
                if _TIME_RANGE_RE.search(str(raw_time or "")):
                    issues.append(
                        {
                            "code": "invalid_time_range",
                            "message": (
                                f"第 {day_index} 天「{label}」的时间"
                                f"「{raw_time}」非法（时或分越界）"
                            ),
                        }
                    )
                continue
            start, end = parsed
            if end <= start:
                # 跨午夜（22:00-02:00 / 20:00-次日01:00）是合法安排；
                # 零长度或超过 12 小时的反向区间（15:00-09:00）仍是非法时间
                if end == start or end + 1440 - start > 12 * 60:
                    issues.append(
                        {
                            "code": "invalid_time_range",
                            "message": (
                                f"第 {day_index} 天「{label}」的结束时间不晚于开始时间"
                            ),
                        }
                    )
                    continue
                end += 1440
            intervals.append((start, end, label))
        intervals.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1]:
                issues.append(
                    {
                        "code": "time_conflict",
                        "message": (
                            f"第 {day_index} 天「{previous[2]}」与「{current[2]}」"
                            "时间重叠"
                        ),
                    }
                )
        # 与前一天跨午夜活动的尾巴比较：只在当天与前一天之间，日期间
        # 不连续（plan 缺天）时同样成立——尾巴本来就是按日历次日算的
        for carried_end, carried_label, carried_day in carry:
            for start, _end, label in intervals:
                if start < carried_end:
                    issues.append(
                        {
                            "code": "time_conflict",
                            "message": (
                                f"第 {carried_day} 天「{carried_label}」与"
                                f"第 {day_index} 天「{label}」时间重叠（跨午夜）"
                            ),
                        }
                    )
        carry = [
            (end - 1440, label, day_index)
            for _start, end, label in intervals
            if end > 1440
        ]

    # 3) + 5) 费用复核：逐项合法性、合计与申报预算一致性
    costs: list[float] = []
    for plan in plans:
        for activity in _activities(plan):
            cost = _activity_cost(activity)
            if cost is None:
                continue
            label = str(
                activity.get("title")
                or activity.get("name")
                or activity.get("activity")
                or ""
            )
            if cost < 0 or cost > 1_000_000:
                issues.append(
                    {
                        "code": "cost_anomaly",
                        "message": f"「{label}」的费用 {cost:g} 元异常（需为 0–1000000）",
                    }
                )
            costs.append(cost)
    item_total = sum(cost for cost in costs if 0 <= cost <= 1_000_000)
    estimated = _estimate(itinerary)
    if costs and estimated is not None and item_total > 0:
        if abs(estimated - item_total) / item_total > 0.2:
            issues.append(
                {
                    "code": "cost_mismatch",
                    "message": (
                        f"申报总费用 {estimated:g} 元与分项合计 {item_total:g} 元"
                        "差异超过 20%"
                    ),
                }
            )

    # 4) 预算
    budget = requirements.get("budget")
    if isinstance(budget, (int, float)) and budget > 0:
        actual = estimated if estimated is not None else item_total
        if actual > budget * 1.02:
            issues.append(
                {
                    "code": "budget_exceeded",
                    "message": f"预计费用 {actual:g} 元超出预算 {budget:g} 元",
                }
            )

    # 5) 必去项：只核对每日活动的名称 / 地点等身份字段。整份 JSON 会把
    # 备注、当天主题、交通说明里顺带提到的地名也算作「已安排」，行程
    # 实际没去也判通过
    must_see = requirements.get("must_see")
    if isinstance(must_see, list):
        parts: list[str] = []
        for plan in plans:
            for activity in _activities(plan):
                for key in (
                    "title",
                    "name",
                    "activity",
                    "location",
                    "place",
                    "景点",
                    "名称",
                    "目的地",
                ):
                    value = activity.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
        text = "\n".join(parts).casefold()
        for name in must_see:
            if str(name).casefold() not in text:
                issues.append(
                    {
                        "code": "must_see_missing",
                        "message": f"必去项「{name}」未出现在行程中",
                    }
                )
    return issues


def format_issues(issues: Iterable[dict[str, str]]) -> str:
    """把校验问题格式化为给模型 / 用户看的编号列表。"""
    lines = [
        f"{index}. [{issue.get('code', 'issue')}] {issue.get('message', '')}"
        for index, issue in enumerate(issues, 1)
    ]
    return "\n".join(lines)
