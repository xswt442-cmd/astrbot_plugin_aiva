"""绑定信息的本地存储。

一个 Bot 用户一份文件，存的是**他自己的登录凭据**：openid、access_token、
userId、tid。这些串都能用来查他的战绩，所以：

- 文件权限收紧到只有属主能读（0o600）；
- 任何对外的地方（repr / 日志 / 异常消息）都不许带凭据本体；
- 解绑即删文件，不留备份。

文件名是用户标识的 SHA-256 摘要：QQ 号、微信 openid 里可能出现斜杠和
`@`，直接拿来拼路径会写错地方甚至越级。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

# 昵称会拼进 PK 提示词。带换行就等于在提示词里凭空多出一行指令；零宽字符
# 还能让两个名字看起来一模一样。存之前就洗掉，读出来用之前再洗一次。
_控制字符 = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202f\u2060\ufeff]")


def 净化昵称(名: str) -> str:
    """去掉控制字符与零宽字符，再压掉首尾空白。"""
    return _控制字符.sub("", str(名 or "")).strip()


__all__ = [
    "凭据仓库",
    "凭据是否齐全",
    "凭据摘要",
    "净化昵称",
    "记绑定时间",
    "记昵称",
    "已绑多久",
    "该提醒重绑",
    "记失效",
    "清失效",
    "刚失效过",
    "找同名的人",
]

_文件后缀 = ".json"

# 腾讯那边 access_token 没有公开的过期字段（登录响应里根本没回 expires_in），
# 只能靠观测反推：实测凭据能撑过几个小时，但跨天后 1003 概率陡增。
# 所以按「绑定期」做提醒，而不是等接口报错才告诉用户——那时候用户已经
# 白等了一轮锐评/战绩。
_绑定期秒 = 12 * 3600

# 必备字段
# 必备字段：所有接口都靠 userId + tid 认人。
# `openid` / `access_token` 是 QQ 扫码那条路给的东西；微信扫码是把授权码交给
# 掌瓦换票，返回里不一定带这两样，所以不能当必备字段。
_必备字段 = ("userId", "tid")

# PK 名字要念进提示词里，太长会挤掉版面。与 `main.py` 的校验同一个值。
_昵称上限 = 16

# 一次失效之后，状态不能靠「这一次恰好成功」就宣布正常。
# 实测：重绑后的头几分钟，新旧会话在服务端交替生效，同一个号会几秒内时好时坏
# （00:39 失效、00:40 成功、7 秒后又失效）。所以记下最近一次失效时间，
# 半小时内按「还没好」处理，等真正取到数据再抹掉。
_失效记忆秒 = 30 * 60


def 记失效(凭据: dict[str, Any], 现在: float | None = None) -> dict[str, Any]:
    """在凭据上盖一个「最近一次登录态失效」的时间戳（就地改）。"""
    凭据["最近失效"] = int(现在 if 现在 is not None else time.time())
    return 凭据


def 清失效(凭据: dict[str, Any]) -> dict[str, Any]:
    """成功取到数据后抹掉时间戳（就地改）。"""
    凭据.pop("最近失效", None)
    return 凭据


def 刚失效过(凭据: dict[str, Any] | None, 现在: float | None = None) -> bool:
    """半小时内失效过就算还没好。没有记录、或记录太旧都算好。"""
    if not 凭据:
        return False
    try:
        时刻 = int(凭据.get("最近失效") or 0)
    except (TypeError, ValueError):
        return False
    if not 时刻:
        return False
    当前 = 现在 if 现在 is not None else time.time()
    return 0 <= 当前 - 时刻 <= _失效记忆秒


def 凭据是否齐全(凭据: dict[str, Any] | None) -> bool:
    if not 凭据:
        return False
    return all(str(凭据.get(字段) or "").strip() for 字段 in _必备字段)


def 凭据摘要(凭据: dict[str, Any]) -> str:
    """给用户看的一行：有 userId 就显示 userId，不带任何令牌。"""
    用户 = str(凭据.get("userId") or "").strip()
    if 用户 and len(用户) > 10:
        return f"{用户[:6]}…{用户[-4:]}"
    return 用户 or "（未知）"


def 记绑定时间(凭据: dict[str, Any]) -> dict[str, Any]:
    """在凭据上盖一个绑定时间戳，返回同一个对象（就地改）。"""
    凭据["绑定于"] = int(time.time())
    return 凭据


def 记昵称(凭据: dict[str, Any], 昵称: str) -> dict[str, Any]:
    """记下平台显示名，PK 时靠它认人（就地改，返回同一对象）。

    **为什么必须有这个**：QQ 官方机器人**收不到 @别人**。
    实测 `raw_message.mentions == []`——平台压根没把被 @ 的人传下来
    （不是适配器过滤掉的，是服务端没给）。组件链里只有 @机器人 一个。
    所以 PK 在 QQ 官方上只能靠名字认人。
    """
    值 = 净化昵称(昵称)
    if 值:
        凭据["昵称"] = 值[:_昵称上限]
    return 凭据


def 找同名的人(仓库: 凭据仓库, 关键词: str, *, 自己: str = "") -> list[tuple[str, str]]:
    """在所有已绑定的人里找昵称匹配 `关键词` 的，返回 `[(用户标识, 昵称)]`。

    **精确优先，模糊兜底**：先把完全相等的挑出来，没有再用子串包含。
    只做子串的话，群里同时有「小明」和「小明同学」，发 `/爱瓦 PK 小明`
    会一次返回两个候选让人选——明明有个一模一样的。爱完美（aiwm）就是
    这么做的，这里照抄。

    比较时忽略大小写并去空格：`znne` 能匹配 `ZnNe`，`老 狗` 也能被 `老狗`
    找到。

    精确命中多于一个（真重名）时把两个都返回，让用户自己选——挑错人等于
    拿错人的战绩，宁可多问一句。
    """
    词 = "".join(str(关键词 or "").split()).lower()
    if not 词:
        return []

    候选: list[tuple[str, str]] = []
    for 用户, 凭据 in 仓库.全部():
        if 自己 and 用户 == 自己:
            continue  # 自己跟自己比没意义
        名 = str(凭据.get("昵称") or "")
        if 名:
            候选.append((用户, 名))

    精确 = [(u, n) for u, n in 候选 if "".join(n.split()).lower() == 词]
    if 精确:
        return 精确
    return [(u, n) for u, n in 候选 if 词 in "".join(n.split()).lower()]


def 已绑多久(凭据: dict[str, Any]) -> int | None:
    """绑了多久（秒）。老数据没这个字段就返回 None。"""
    时刻 = 凭据.get("绑定于")
    if not isinstance(时刻, int):
        return None
    return max(0, int(time.time()) - 时刻)


def 该提醒重绑(凭据: dict[str, Any]) -> bool:
    """凭据是不是老到该提醒重绑了。"""
    多久 = 已绑多久(凭据)
    return 多久 is not None and 多久 > _绑定期秒


class 凭据仓库:
    def __init__(self, 目录: Path) -> None:
        self.目录 = 目录

    def 路径(self, 用户: str) -> Path:
        摘要 = hashlib.sha256(用户.encode("utf-8")).hexdigest()[:32]
        return self.目录 / f"{摘要}{_文件后缀}"

    def 读取(self, 用户: str) -> dict[str, Any] | None:
        路径 = self.路径(用户)
        try:
            载荷 = json.loads(路径.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return 载荷 if isinstance(载荷, dict) else None

    def 写入(self, 用户: str, 凭据: dict[str, Any]) -> None:
        路径 = self.路径(用户)
        路径.parent.mkdir(parents=True, exist_ok=True)
        # 文件名是 SHA-256 摘要，**反查不出用户标识**。要按昵称找人就必须
        # 把它明文存进文件里（`用户标识`），否则只能靠遍历比对哈希。
        凭据["用户标识"] = 用户
        载荷 = json.dumps(凭据, ensure_ascii=False, indent=2)
        # 临时文件 + 替换：写到一半崩了也不会留下半个凭据文件。
        临时 = 路径.with_suffix(f"{_文件后缀}.tmp")
        临时.write_text(载荷, encoding="utf-8")
        os.replace(临时, 路径)
        try:
            os.chmod(路径, 0o600)
        except OSError:
            # Windows 上 chmod 不是完整 ACL；任务失败不影响可用性。
            pass

    def 全部(self) -> list[tuple[str, dict[str, Any]]]:
        """列出所有绑定，返回 `[(用户标识, 凭据)]`。

        老文件（`用户标识`/`昵称` 都在本版本才开始记）会**被跳过**：
        它们既认不出人也没名字，对 PK / 排行榜毫无用处。重新绑定一次就有了。
        """
        出 = []
        try:
            条目 = sorted(self.目录.glob(f"*{_文件后缀}"))
        except OSError:
            return 出
        for 路径 in 条目:
            try:
                凭据 = json.loads(路径.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(凭据, dict):
                continue
            用户 = str(凭据.get("用户标识") or "").strip()
            if 用户 and self.路径(用户) == 路径:
                出.append((用户, 凭据))
        return 出

    def 删除(self, 用户: str) -> bool:
        路径 = self.路径(用户)
        # 写盘是「先写 .json.tmp 再 os.replace」。中途崩掉会留下临时文件，
        # 里面同样是一份完整凭据，解绑时一并清掉。
        临时 = 路径.with_suffix(f"{_文件后缀}.tmp")
        try:
            路径.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        finally:
            try:
                临时.unlink(missing_ok=True)
            except OSError:
                pass
        return True
