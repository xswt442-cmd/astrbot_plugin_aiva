"""PK 的对手解析。

对手身份从消息里捡，按可靠性排：

1. **`raw_message.msg_elements` / `raw_data`** —— QQ 官方平台的原始报文。
   **这条尚未在线上验证过**（见下面「实测结论」）。
2. `raw_message.mentions` —— botpy 解析出来的 mention 列表。
3. At 组件的 `qq` —— 别的平台（OneBot 等）会把 @ 转成组件。
4. 正文里的 `<@!id>` —— 少数平台保留这种写法。
5. **按昵称匹配** —— 前四条都拿不到时的兜底，由 `main.py` 的 PK 指令接管。

## 实测结论：QQ 官方机器人收不到 @别人

线上日志（2026-10-13 22:28，用户截图里明确 @ 了群友 YJJ）：

```
[aiva] PK 找对手：自己='2A8F...'
  raw_message=PatchedGroupMessage
  mentions=[]            ← 空数组
  组件链=[At, Plain]      ← 只有一个 At，是 @机器人
  message_str='爱瓦 PK'    ← @YJJ 连文本都没留下
```

`mentions` **是空的**。所以不是适配器过滤掉的（适配器只对 `is_you=True`
的做转换，不会让别人凭空消失），而是**平台压根没把被 @ 的人传过来**。

适配器里有 `abm.raw_message = message`，且 `_set_raw_message_fields()` 额外
挂了 `raw_data`（QQ 原始 payload）和 `msg_elements`。@ 元素最可能在这两个
里，所以先试它们；万一都没有，就只能按昵称认人。

**教训**：这条指令最初把 @ 当成唯一入口，失败两次都没日志，只能靠猜字段名。
以后任何「从消息里取 X」的逻辑，取不到时**必须打日志**，这是唯一能收敛
假设的办法——两次白跑都是因为日志里什么都没有。

拿不到时给用户明确的话，而不是随便挑一个人比——比错人等于泄露别人的战绩。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "FAKE_MENTION_IDS",
    "find_opponent",
    "mention_summary",
    "opponent_from_mentions",
    "opponent_from_raw",
]

# `<@1234>` 与 `<@!1234>` 两种写法都存在。长度下限用来滤掉 `<@1>` 这类噪声。
_BODY_MENTION = re.compile(r"<@!?([A-Za-z0-9_-]{6,})>")

# 这些值在 At.qq 里出现过，都不是真实用户。
FAKE_MENTION_IDS = frozenset({"qq_official", "all", "unknown_selfid"})


def _usable(candidate: Any, excluded: set[str]) -> str | None:
    value = str(candidate or "").strip()
    if not value or value in FAKE_MENTION_IDS or value in excluded:
        return None
    return value


def opponent_from_mentions(raw_message: Any, *, me: str = "", bot_id: str = "") -> str | None:
    """从 `raw_message.mentions` 里找对手，找不到返回 None。

    跳过 `is_you` 的（那是 @机器人）和 `bot` 的。

    注意：QQ 官方平台上实测这个数组是**空的**（见模块 docstring），所以
    别把它当唯一入口。
    """
    excluded = {value for value in (me, bot_id) if value}
    for mention in getattr(raw_message, "mentions", None) or []:
        if getattr(mention, "is_you", False):
            continue
        if getattr(mention, "bot", False):
            continue
        candidate = _usable(_mention_id(mention), excluded)
        if candidate:
            return candidate
    return None


def _mention_id(item: Any) -> str:
    """从一个 mention 对象里取用户标识。

    `botpy` 的 mention 有 `id` / `member_openid` / `user_openid` 三个可能的
    字段，谁有值取谁——`member_openid` 才是群里的用户标识，也正是
    `get_sender_id()` 返回的那个。
    """
    for field in ("member_openid", "id", "user_openid"):
        value = str(getattr(item, field, "") or "").strip()
        if value:
            return value
    return ""


def mention_summary(raw_message: Any) -> list[dict[str, Any]]:
    """把 mentions / msg_elements 压成可读的摘要，给日志用。"""
    rows = []
    for mention in getattr(raw_message, "mentions", None) or []:
        rows.append(
            {
                "member_openid": getattr(mention, "member_openid", None),
                "id": getattr(mention, "id", None),
                "username": getattr(mention, "username", None),
                "is_you": getattr(mention, "is_you", None),
                "bot": getattr(mention, "bot", None),
            }
        )
    return rows


def opponent_from_raw(raw_message: Any, *, me: str = "", bot_id: str = "") -> str | None:
    """从 QQ 原始 payload（`raw_data` / `msg_elements`）里找被 @ 的人。

    这是 QQ 官方平台上最有可能拿到对手的地方：`mentions` 实测为空，
    但适配器 `_set_raw_message_fields` 把 QQ 的原始报文留在了
    `raw_message.raw_data` 和 `msg_elements` 上。

    认得的形态全试一遍，认不出就返回 None（别猜）：
    - `msg_elements` 里 `element_type == "mention"` 的 `user_id` /
      `member_openid` / `qq`；
    - `raw_data` 里 `mentions` / `at_users` 之类数组的 `member_openid`。
    """
    excluded = {value for value in (me, bot_id) if value}

    for element in getattr(raw_message, "msg_elements", None) or []:
        if not isinstance(element, dict):
            continue
        if str(element.get("element_type") or "").lower() != "mention":
            continue
        # mention 元素的 user_id 可能是 QQ 号，也可能是 openid
        for field in ("member_openid", "user_id", "id", "qq", "user_openid"):
            candidate = _usable(element.get(field), excluded)
            if candidate:
                return candidate

    payload = getattr(raw_message, "raw_data", None)
    if isinstance(payload, dict):
        for field in ("mentions", "at_users", "mention_list", "at_list"):
            array = payload.get(field)
            if not isinstance(array, list):
                continue
            for item in array:
                if isinstance(item, dict):
                    candidate = _usable(
                        item.get("member_openid")
                        or item.get("id")
                        or item.get("user_id")
                        or item.get("user_openid"),
                        excluded,
                    )
                else:
                    candidate = _usable(item, excluded)
                if candidate:
                    return candidate
    return None


def find_opponent(components: Any, text: str, *, me: str = "", bot_id: str = "") -> str | None:
    """从组件链和正文里找 PK 的对手标识，找不到返回 None。

    `me` 与 `bot_id` 都得排掉：群里 @机器人 同样会生成一个 At 组件，
    不排掉的话「@机器人 PK @小明」会把机器人当成 PK 对象。
    """
    excluded = {value for value in (me, bot_id) if value}

    for component in components or []:
        candidate = _usable(getattr(component, "qq", None), excluded)
        if candidate:
            return candidate

    for hit in _BODY_MENTION.finditer(text or ""):
        candidate = _usable(hit.group(1), excluded)
        if candidate:
            return candidate

    return None
