"""绑定信息的本地存储。

一个 Bot 用户一份文件，存的是**他自己的登录凭据**：openid、access_token、
userId、tid。这些串都能用来查他的战绩，所以：

- 文件权限收紧到只有属主能读（0o600）；
- 任何对外的地方（repr / 日志 / 异常消息）都不许带凭据本体；
- 解绑即删文件，不留备份。

文件名是用户标识的 SHA-256 摘要：QQ 号、微信 openid 里可能出现斜杠和
`@`，直接拿来拼路径会写错地方甚至越级。

**这里的中文只出现在字符串里**：`"昵称"` / `"绑定于"` / `"最近失效"` /
`"用户标识"` 是磁盘上已有的键，改了要迁移所有用户的文件，所以保持原样。
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
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202f\u2060\ufeff]")


def clean_nickname(name: str) -> str:
    """去掉控制字符与零宽字符，再压掉首尾空白。"""
    return _CONTROL_CHARS.sub("", str(name or "")).strip()


__all__ = [
    "CredentialStore",
    "clear_expired",
    "find_by_nickname",
    "is_complete",
    "mark_expired",
    "masked_id",
    "recently_expired",
    "seconds_since_bind",
    "set_nickname",
    "should_rebind",
    "stamp_bound_at",
    "clean_nickname",
]

_FILE_SUFFIX = ".json"

# 腾讯那边 access_token 没有公开的过期字段（登录响应里根本没回 expires_in），
# 只能靠观测反推：实测凭据能撑过几个小时，但跨天后 1003 概率陡增。
# 所以按「绑定期」做提醒，而不是等接口报错才告诉用户——那时候用户已经
# 白等了一轮锐评/战绩。
_REBIND_HINT_SECONDS = 12 * 3600

# 必备字段：所有接口都靠 userId + tid 认人。
# `openid` / `access_token` 是 QQ 扫码那条路给的东西；微信扫码是把授权码交给
# 掌瓦换票，返回里不一定带这两样，所以不能当必备字段。
_REQUIRED_FIELDS = ("userId", "tid")

# PK 名字要念进提示词里，太长会挤掉版面。与 `main.py` 的校验同一个值。
_NICKNAME_LIMIT = 16

# 一次失效之后，状态不能靠「这一次恰好成功」就宣布正常。
# 实测：重绑后的头几分钟，新旧会话在服务端交替生效，同一个号会几秒内时好时坏
# （00:39 失效、00:40 成功、7 秒后又失效）。所以记下最近一次失效时间，
# 半小时内按「还没好」处理，等真正取到数据再抹掉。
_EXPIRY_MEMORY_SECONDS = 30 * 60


def mark_expired(credential: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """在凭据上盖一个「最近一次登录态失效」的时间戳（就地改）。"""
    credential["最近失效"] = int(now if now is not None else time.time())
    return credential


def clear_expired(credential: dict[str, Any]) -> dict[str, Any]:
    """成功取到数据后抹掉时间戳（就地改）。"""
    credential.pop("最近失效", None)
    return credential


def recently_expired(credential: dict[str, Any] | None, now: float | None = None) -> bool:
    """半小时内失效过就算还没好。没有记录、或记录太旧都算好。"""
    if not credential:
        return False
    try:
        stamp = int(credential.get("最近失效") or 0)
    except (TypeError, ValueError):
        return False
    if not stamp:
        return False
    current = now if now is not None else time.time()
    return 0 <= current - stamp <= _EXPIRY_MEMORY_SECONDS


def is_complete(credential: dict[str, Any] | None) -> bool:
    if not credential:
        return False
    return all(str(credential.get(field) or "").strip() for field in _REQUIRED_FIELDS)


def masked_id(credential: dict[str, Any]) -> str:
    """给用户看的一行：有 userId 就显示 userId，不带任何令牌。"""
    user_id = str(credential.get("userId") or "").strip()
    if user_id and len(user_id) > 10:
        return f"{user_id[:6]}…{user_id[-4:]}"
    return user_id or "（未知）"


def stamp_bound_at(credential: dict[str, Any]) -> dict[str, Any]:
    """在凭据上盖一个绑定时间戳，返回同一个对象（就地改）。"""
    credential["绑定于"] = int(time.time())
    return credential


def set_nickname(credential: dict[str, Any], nickname: str) -> dict[str, Any]:
    """记下平台显示名，PK 时靠它认人（就地改，返回同一对象）。

    **为什么必须有这个**：QQ 官方机器人**收不到 @别人**。
    实测 `raw_message.mentions == []`——平台压根没把被 @ 的人传下来
    （不是适配器过滤掉的，是服务端没给）。组件链里只有 @机器人 一个。
    所以 PK 在 QQ 官方上只能靠名字认人。
    """
    value = clean_nickname(nickname)
    if value:
        credential["昵称"] = value[:_NICKNAME_LIMIT]
    return credential


def find_by_nickname(
    store: CredentialStore, keyword: str, *, me: str = ""
) -> list[tuple[str, str]]:
    """在所有已绑定的人里找昵称匹配 `keyword` 的，返回 `[(用户标识, 昵称)]`。

    **精确优先，模糊兜底**：先把完全相等的挑出来，没有再用子串包含。
    只做子串的话，群里同时有「小明」和「小明同学」，发 `/爱瓦 PK 小明`
    会一次返回两个候选让人选——明明有个一模一样的。爱完美（aiwm）就是
    这么做的，这里照抄。

    比较时忽略大小写并去空格：`znne` 能匹配 `ZnNe`，`老 狗` 也能被 `老狗`
    找到。

    精确命中多于一个（真重名）时把两个都返回，让用户自己选——挑错人等于
    拿错人的战绩，宁可多问一句。
    """
    needle = "".join(str(keyword or "").split()).lower()
    if not needle:
        return []

    candidates: list[tuple[str, str]] = []
    for user_id, credential in store.iter_all():
        if me and user_id == me:
            continue  # 自己跟自己比没意义
        name = str(credential.get("昵称") or "")
        if name:
            candidates.append((user_id, name))

    exact = [(u, n) for u, n in candidates if "".join(n.split()).lower() == needle]
    if exact:
        return exact
    return [(u, n) for u, n in candidates if needle in "".join(n.split()).lower()]


def seconds_since_bind(credential: dict[str, Any]) -> int | None:
    """绑了多久（秒）。老数据没这个字段就返回 None。"""
    stamp = credential.get("绑定于")
    if not isinstance(stamp, int):
        return None
    return max(0, int(time.time()) - stamp)


def should_rebind(credential: dict[str, Any]) -> bool:
    """凭据是不是老到该提醒重绑了。"""
    age = seconds_since_bind(credential)
    return age is not None and age > _REBIND_HINT_SECONDS


class CredentialStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, user_id: str) -> Path:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        return self.root / f"{digest}{_FILE_SUFFIX}"

    def read(self, user_id: str) -> dict[str, Any] | None:
        path = self.path_for(user_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def write(self, user_id: str, credential: dict[str, Any]) -> None:
        path = self.path_for(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 文件名是 SHA-256 摘要，**反查不出用户标识**。要按昵称找人就必须
        # 把它明文存进文件里（`用户标识`），否则只能靠遍历比对哈希。
        credential["用户标识"] = user_id
        payload = json.dumps(credential, ensure_ascii=False, indent=2)
        # 临时文件 + 替换：写到一半崩了也不会留下半个凭据文件。
        tmp_path = path.with_suffix(f"{_FILE_SUFFIX}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows 上 chmod 不是完整 ACL；任务失败不影响可用性。
            pass

    def iter_all(self) -> list[tuple[str, dict[str, Any]]]:
        """列出所有绑定，返回 `[(用户标识, 凭据)]`。

        老文件（`用户标识`/`昵称` 都在本版本才开始记）会**被跳过**：
        它们既认不出人也没名字，对 PK / 排行榜毫无用处。重新绑定一次就有了。
        """
        rows = []
        try:
            entries = sorted(self.root.glob(f"*{_FILE_SUFFIX}"))
        except OSError:
            return rows
        for path in entries:
            try:
                credential = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(credential, dict):
                continue
            user_id = str(credential.get("用户标识") or "").strip()
            if user_id and self.path_for(user_id) == path:
                rows.append((user_id, credential))
        return rows

    def delete(self, user_id: str) -> bool:
        path = self.path_for(user_id)
        # 写盘是「先写 .json.tmp 再 os.replace」。中途崩掉会留下临时文件，
        # 里面同样是一份完整凭据，解绑时一并清掉。
        tmp_path = path.with_suffix(f"{_FILE_SUFFIX}.tmp")
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return True
