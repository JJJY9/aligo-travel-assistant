"""请求级「当前时刻」口径。

服务器常以 UTC 部署（README 建议容器 TZ=UTC），而用户说的「今天 / 明天 /
下周一」按浏览器本地日期理解：提示词注入服务器日期会让东八区用户在
本地 00:00–08:00 的提问整体早一天，行程以错误日期落库。

请求入口按上报时区绑定一次后，提示词构建与相对日期归一都从同一口径取
「现在」；CLI / 未绑定时回退进程本地时间（既有行为不变）。用 contextvar
而不是改函数签名，避免把时区一路透传到每个技能。
"""
from __future__ import annotations

import contextvars
from datetime import date, datetime, tzinfo

_REQUEST_TZ: contextvars.ContextVar[tzinfo | None] = contextvars.ContextVar(
    "aligo_request_tz", default=None
)


def use_timezone(zone: tzinfo | None) -> contextvars.Token:
    """绑定本次请求的时区；调用方结束时应 ``reset`` 归还。"""
    return _REQUEST_TZ.set(zone)


def reset(token: contextvars.Token) -> None:
    """恢复绑定前的时区。"""
    _REQUEST_TZ.reset(token)


def current_timezone() -> tzinfo | None:
    """当前请求绑定的时区（未绑定返回 None）。"""
    return _REQUEST_TZ.get()


def now() -> datetime:
    """当前时刻：已绑定请求时区用该时区的 aware 值，否则进程本地 naive。"""
    zone = _REQUEST_TZ.get()
    return datetime.now(zone) if zone is not None else datetime.now()


def today() -> date:
    """当前日期（与 :func:`now` 同一口径）。"""
    return now().date()
