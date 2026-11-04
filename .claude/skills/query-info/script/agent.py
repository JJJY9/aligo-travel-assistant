"""
信息查询智能体 - 真实检索版（免费API）
支持：天气（Open-Meteo）、网络搜索（DDGS，开启 safesearch + 结果过滤）

使用免费API：
- 天气：Open-Meteo（无需 API Key，国内可达性好）
- 搜索：ddgs（Dux Distributed Global Search，可选 bing/duckduckgo 等，需安装：pip install ddgs）
"""
import json
import logging
import os
import re
import sys
from typing import Any

from agentscope.agent import AgentBase
from agentscope.message import Msg

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.json_parser import extract_json_from_async_response

logger = logging.getLogger(__name__)

# 尝试导入 duckduckgo_search (旧包名) 或 ddgs (新包名)
try:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False
    logger.warning("ddgs not installed. Install with: pip install ddgs")

# 疑似垃圾/低质域名：多为 SEO 或不良站，不展示给用户
_SUSPICIOUS_DOMAIN_PATTERN = re.compile(
    r"\.(cc|tk|ml|ga|cf|gq|xyz|top|work|click|link|pw|buzz)(/|$)",
    re.IGNORECASE
)
# 域名主体若为长随机字母（无明显词），则过滤
_RANDOM_DOMAIN_PATTERN = re.compile(r"^[a-z0-9]{10,}$", re.IGNORECASE)


def _is_suspicious_url(url: str) -> bool:
    """过滤疑似垃圾/不良站点（如部分 .cc/.tk 等易被滥用的域名）。"""
    if not url or not url.startswith("http"):
        return True
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc or ""
        # 去掉端口
        host = host.split(":")[0].lower()
        if not host:
            return True
        # 可疑 TLD
        if _SUSPICIOUS_DOMAIN_PATTERN.search(host):
            return True
        # 主域名部分（最后一个 . 之前若还有多段则取倒数第二段之前）
        parts = host.rsplit(".", 2)
        name = parts[0] if parts else ""
        return bool(len(name) >= 10 and _RANDOM_DOMAIN_PATTERN.match(name))
    except Exception:
        return False


class InformationQueryAgent(AgentBase):
    """
    信息查询智能体（真实检索版）

    核心功能：
    - 天气查询 - 使用 Open-Meteo 免费 API（无需 Key / 无需搜索）
    - 网络搜索 - 使用 DDGS（开启 safesearch，过滤可疑来源）

    注意：
    - 差旅标准查询由独立的 RAGKnowledgeAgent 处理
    """

    def __init__(self, name: str = "InformationQueryAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

    async def reply(self, x: Msg | list[Msg] | None = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content=json.dumps({"query_success": False}), role="assistant")

        # 解析输入
        content = x.content if not isinstance(x, list) else x[-1].content

        if isinstance(content, str):
            try:
                data = json.loads(content)
                context = data.get("context", {})
                user_query = context.get("rewritten_query", "") or content
            except json.JSONDecodeError:
                user_query = content
        else:
            user_query = str(content)

        # 天气类问题优先走 Open-Meteo；接口不可用时回退到联网搜索
        result = None
        if self._is_weather_query(user_query):
            logger.info(f"Weather query: {user_query}")
            try:
                result = await self._weather_query(user_query)
            except Exception as e:
                logger.warning(f"Weather query failed, fallback to web search: {e}")
                result = None
            if result is not None and result.get("query_success"):
                return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
            logger.warning("Weather API unavailable, falling back to web search")

        if result is None or not result.get("query_success"):
            logger.info(f"Web search query: {user_query}")
            try:
                result = await self._web_search(user_query)
            except Exception as e:
                logger.error(f"Query failed: {e}")
                result = {
                    "query_type": "网络搜索",
                    "query_success": False,
                    "results": {"error": str(e)},
                }

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    def _is_weather_query(self, query: str) -> bool:
        """简单判断是否为天气类问题。"""
        q = (query or "").strip()
        if not q:
            return False
        return "天气" in q or "气温" in q or "下雨" in q or "预报" in q

    # WMO 天气代码 → 中文描述（Open-Meteo 使用 WMO 4677 标准）
    _WEATHER_CODE_MAP: dict[int, str] = {
        0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
        45: "有雾", 48: "雾凇",
        51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨",
        66: "冻雨", 67: "强冻雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "阵雨", 81: "强阵雨", 82: "暴雨",
        85: "阵雪", 86: "强阵雪",
        95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
    }

    async def _http_json(self, url: str, params: dict[str, Any]) -> dict:
        """在 executor 中执行 GET 并返回 JSON（避免阻塞事件循环）。"""
        import asyncio

        import httpx

        def _get() -> dict:
            resp = httpx.get(
                url,
                params=params,
                timeout=10.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AligoTravel/1.0)"},
            )
            resp.raise_for_status()
            return resp.json()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get)

    async def _weather_query(self, query: str) -> dict[str, Any]:
        """
        使用 Open-Meteo 免费 API 查询天气（无需 API Key，国内网络可达性优于 wttr.in）。

        流程：城市名 → 地理编码 → 实时天气 + 未来 3 天预报。
        """
        city = self._extract_city_from_query(query)
        if not city:
            return {
                "query_type": "天气查询",
                "query_success": False,
                "results": {"message": "未识别到城市，请说明具体城市，如：杭州下周的天气怎么样？"},
            }

        try:
            geo = await self._http_json(
                "https://geocoding-api.open-meteo.com/v1/search",
                {"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            matches = geo.get("results") or []
            if not matches:
                return {
                    "query_type": "天气查询",
                    "query_success": False,
                    "results": {"message": f"未找到城市「{city}」，请检查城市名"},
                }
            location = matches[0]
            city_name = location.get("name") or city

            data = await self._http_json(
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,weather_code",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "forecast_days": 3,
                },
            )
        except Exception as e:
            logger.warning(f"Open-Meteo request failed: {e}")
            return {
                "query_type": "天气查询",
                "query_success": False,
                "results": {
                    "message": f"天气接口暂时不可用: {e}",
                    "sources": [{"url": "https://open-meteo.com", "title": "Open-Meteo"}],
                },
            }

        try:
            current = data.get("current", {})
            desc = self._WEATHER_CODE_MAP.get(current.get("weather_code"), "未知")
            weather_text = (
                f"{city_name}当前天气：{desc}，气温 {current.get('temperature_2m', '?')}°C，"
                f"湿度 {current.get('relative_humidity_2m', '?')}%。"
            )
            daily = data.get("daily", {})
            dates = daily.get("time", [])[:3]
            codes = daily.get("weather_code", [])
            lows = daily.get("temperature_2m_min", [])
            highs = daily.get("temperature_2m_max", [])
            forecasts = []
            for i, date in enumerate(dates):
                day_desc = self._WEATHER_CODE_MAP.get(codes[i] if i < len(codes) else None, "未知")
                low = lows[i] if i < len(lows) else "?"
                high = highs[i] if i < len(highs) else "?"
                forecasts.append(f"{date}: {day_desc}，{low}~{high}°C")
            if forecasts:
                weather_text += " 未来几日：" + "；".join(forecasts)
            return {
                "query_type": "天气查询",
                "query_success": True,
                "results": {
                    "summary": weather_text,
                    "sources": [{"url": "https://open-meteo.com", "title": "Open-Meteo"}],
                },
            }
        except Exception as e:
            logger.warning(f"Parse Open-Meteo response failed: {e}")
            return {
                "query_type": "天气查询",
                "query_success": False,
                "results": {
                    "message": "天气数据解析失败",
                    "sources": [{"url": "https://open-meteo.com", "title": "Open-Meteo"}],
                },
            }

    def _extract_city_from_query(self, query: str) -> str:
        """从问题中提取城市名：先匹配常见城市，再取关键词前的连续中文字。"""
        common_cities = [
            "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安", "苏州",
            "天津", "重庆", "厦门", "青岛", "大连", "宁波", "无锡", "长沙", "郑州", "济南",
            "哈尔滨", "沈阳", "昆明", "合肥", "福州", "石家庄", "南昌", "贵阳", "太原", "南宁",
        ]
        q = (query or "").strip()
        for city in common_cities:
            if city in q:
                return city
        # 去掉时间词与问句措辞，取剩余文本中第一段 2~6 个连续中文字
        head = re.split(r"(?:的?天气|气温|下雨|预报|怎么样|如何|情况)", q, maxsplit=1)[0]
        head = re.sub(r"(明天|今天|后天|昨天|周[一二三四五六日末]|下周|这周|未来|这几天|近期)", "", head)
        match = re.search(r"[一-龥]{2,6}", head)
        return match.group(0).strip() if match else ""

    async def _web_search(self, query: str) -> dict[str, Any]:
        """
        网络搜索 - 使用 DDGS（Dux Distributed Global Search），开启 safesearch，过滤可疑来源。

        Args:
            query: 用户查询

        Returns:
            搜索结果
        """
        if not DDGS_AVAILABLE:
            return {
                "query_type": "网络搜索",
                "query_success": False,
                "results": {
                    "message": "搜索库未安装",
                    "note": "请运行：pip install ddgs",
                },
            }

        try:
            import asyncio

            ddgs = DDGS()
            # 开启安全搜索，优先 bing 后端（质量更稳定），多取几条再过滤
            search_results = []
            loop = asyncio.get_running_loop()
            for backend in ("bing", "duckduckgo", "auto"):
                try:
                    def _search(backend_name: str = backend) -> list:
                        return list(
                            ddgs.text(
                                query,
                                max_results=10,
                                safesearch="on",
                                region="cn-zh",
                                backend=backend_name,
                            )
                        )

                    # ddgs.text 是同步网络调用，必须放到线程池，
                    # 否则会卡住事件循环（SSE 心跳与其它会话全部停顿）
                    search_results = await loop.run_in_executor(None, _search)
                    if search_results:
                        break
                except Exception as e:
                    logger.debug(f"DDGS backend {backend} failed: {e}")
                    continue

            results = []
            for result in search_results:
                href = result.get("href", "")
                if _is_suspicious_url(href):
                    continue
                results.append({
                    "title": result.get("title", ""),
                    "snippet": result.get("body", ""),
                    "url": href,
                })
                if len(results) >= 5:
                    break

            if not results:
                return {
                    "query_type": "网络搜索",
                    "query_success": False,
                    "results": {"message": "未找到相关结果"},
                }

            # 使用 LLM 总结搜索结果
            summary = await self._summarize_search_results(query, results)

            return {
                "query_type": "网络搜索",
                "query_success": True,
                "results": {
                    "summary": summary,
                    "sources": results,
                },
            }
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return {
                "query_type": "网络搜索",
                "query_success": False,
                "results": {"error": f"搜索失败: {e!s}"},
            }

    async def _summarize_search_results(self, query: str, results: list[dict]) -> str:
        """
        使用 LLM 总结搜索结果

        Args:
            query: 用户查询
            results: 搜索结果列表

        Returns:
            总结文本
        """
        if not results:
            return "未找到相关信息"

        # 构建搜索结果文本
        results_text = ""
        for i, result in enumerate(results, 1):
            results_text += f"\n{i}. {result['title']}\n{result['snippet']}\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 动态读取 Prompt 指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("query-info")
        if not skill_instruction:
            skill_instruction = "请直接回答用户的问题，保持简洁。"

        prompt = f"""根据以下搜索结果，简洁地回答用户的问题。

【当前时间】
{current_date} {weekday}
（用户查询中的相对时间请基于此日期理解，如"明天"、"2月28日"等）

【用户问题】
{query}

【搜索结果】
{results_text}

【任务说明】
{skill_instruction}
"""

        try:
            response = await self.model([{"role": "user", "content": prompt}])

            # 获取响应文本（兼容非流式 ChatResponse / 流式生成器 / 测试 mock）
            text = await extract_json_from_async_response(response)

            return text.strip() if text else "无法生成摘要"
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return "搜索成功，但摘要生成失败"
