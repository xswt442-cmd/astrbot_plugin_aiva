"""爱瓦：无畏契约国服战绩查询。

用户扫码绑定自己的掌上无畏契约账号后，指令与模型都能读到他自己的战绩数据。

数据来自 `app.mval.qq.com`（掌上无畏契约 App 的私有接口）。国服没有官方公开
API：Riot 官方 API 只覆盖国际服，`api.riotgames.cn` 实测 DNS 无记录。所以
这条链路**必须有登录态**，也因此免不了跟着 App 版本走——`mcode` 失效的那天
所有请求会一起报错，文案已经翻译成人话（见 `aiva/transport.py`）。
"""

from __future__ import annotations

import asyncio
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import command_group, custom_filter, llm_tool
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.custom_filter import CustomFilter

# 全部在模块顶层导入。插件加载时就把整个包摸清：少文件、依赖缺失都在启动
# 阶段报错，而不是等用户发了指令才在懒加载里抛模块不存在。
from .aiva.auth.ptlogin import QrSession, redact
from .aiva.auth.wechat_flow import WechatQrSession
from .aiva.fallback import (
    is_bare_command,
    is_command_miss,
    leftover_arg,
    miss_hint,
    starts_with_group,
)
from .aiva.match import build_match_setup, setup_text
from .aiva.pk import find_opponent, mention_summary, opponent_from_mentions, opponent_from_raw
from .aiva.prompts import match_prompt, pk_prompt, review_prompt
from .aiva.stats import advantage_count, aggregate, contrast_points, contrast_text, recent_text
from .aiva.storage import (
    CredentialStore,
    clean_nickname,
    clear_expired,
    find_by_nickname,
    is_complete,
    mark_expired,
    masked_id,
    recently_expired,
    seconds_since_bind,
    set_nickname,
    stamp_bound_at,
)
from .aiva.transport import (
    ApiError,
    LoginExpired,
    MvalClient,
    fetch_matches,
    set_log_hook,
    tier_of,
)

_GROUP = "爱瓦"

# 组名别名。用户习惯写 `/爱瓦 绑定`，但 QQ 官方适配器在「@机器人 + 指令」
# 这条路径上走的是 At 唤醒分支——`waking_check/stage.py` 的At 分支只置
# `wake_prefix = ""`，**不剥掉 message_str 里的 `/`**（只有 startswith 分支才剥）。
# 于是 message_str 仍是 `/爱瓦 绑定`，组名 `爱瓦` 匹配不上，整条指令静默失败。
# 加一个带斜杠的别名，两种写法都能进。
_GROUP_ALIASES = {f"/{_GROUP}"}

# 数据目录必须用插件名。市场审核：「数据持久化位置不符合要求」，规则是
# `data/plugin_data/<plugin_name>`，而 `<plugin_name>` 指插件目录名
# （`astrbot_plugin_aiva`），不是自造的简称 `aiva`。v0.2.4 就是被这一条驳回的。
_PLUGIN_NAME = "astrbot_plugin_aiva"
# 0.2.4 及以前用的是这个简称目录，升级时把凭据搬过来。
_LEGACY_DATA_DIR = "aiva"


def _migrate_legacy_credentials(data_dir: Path) -> None:
    """把 `plugin_data/aiva` 里的凭据搬到 `plugin_data/astrbot_plugin_aiva`。

    只搬不覆盖：新目录已有同名文件就保留新的。搬完把空掉的旧目录删掉——
    留着它，下次审核还会看到数据在旧位置。
    """
    legacy_dir = data_dir.parent / _LEGACY_DATA_DIR
    if not legacy_dir.is_dir() or legacy_dir.resolve() == data_dir.resolve():
        return

    for file in sorted(legacy_dir.glob("*.json")):
        target = data_dir / file.name
        if target.exists():
            continue
        try:
            shutil.move(str(file), str(target))
            target.chmod(0o600)
        except OSError as exc:
            logger.warning(f"[aiva] 迁移旧凭据失败 {file.name}：{exc}")

    try:
        legacy_dir.rmdir()
    except OSError:
        pass


# 扫码轮询：目标节奏每 2 秒一次，但**按墙钟掐表**，不按次数。
#
# 出码到二维码失效约 60 秒（从出码那一刻起算，不是从第一轮轮询起算）。
# 原来写的是「先 sleep(2) 再发请求」，而服务器到腾讯这一跳实测要 ~3 秒，
# 于是每轮实际占 5 秒，25 轮拉成 125 秒——等问到第 20 轮时腾讯早把码作废了，
# 白白让用户对着废码扫半天。现在改成：记下截止时刻，每轮补足到下一个节奏点，
# 网络慢就从预算里扣，慢到超预算就立刻收工。
_POLL_INTERVAL = 2.0
_POLL_BUDGET = 55.0

# 调模型的最长等待。`tool_loop_agent` 没有内置超时，上游挂起时它就一直等，
# 用户看到的是「机器人死了」，我们连卡在哪一步都不知道。
# 实测正常一次 20~30 秒（max_steps=8，可能多轮工具调用），90 秒留足余量：
# 超了就是上游真有问题，早点失败比让人干等强。
_MODEL_TIMEOUT = 90.0


def _config_int(config: Any, key: str, default: int) -> int:
    """从配置里取正整数。缺项、写错、非正数都退回默认值。"""
    if config is None:
        return default
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _config_flag(config: Any, key: str, default: bool) -> bool:
    if config is None:
        return default
    try:
        return bool(config.get(key, default))
    except Exception:  # noqa: BLE001 - 配置读不到就当默认
        return default


# 掌瓦的登录态有固定有效期：实测票据 `validTo - validFrom` 正好 259200 秒（三天）。
# 到期后战绩接口一律拒绝，重新扫码就能续上。
_LOGIN_EXPIRED_HINT = "登录态失效，重新发 /爱瓦 绑定。掌瓦的登录态大约三天到期。"


# 直连 provider 时给的系统提示。原来的提示全写在 user 消息里（`prompts.py`
# 生成的 927 字），拆出一层 system 只是为了把「你是谁」和「这次要干什么」
# 分开——省不了多少字，但模型对 system 段的指令遵循明显更稳。
_MODEL_SYSTEM_PROMPT = (
    "你在给一个《无畏契约》国服玩家写数据点评。"
    "你只能使用用户消息里给出的统计数字，不要编造任何没给出的数据、"
    "段位、地图或特工。严格按用户要求的格式输出，不要加前言、标题或编号。"
)

_HELP_TEXT = """爱瓦 · 无畏契约国服战绩
/爱瓦 绑定
QQ 扫码绑定你的无畏契约账号。
/爱瓦 绑定微信
微信扫码绑定，账号在微信区时用这个。
/爱瓦 战绩
查询近期战绩。
/爱瓦 锐评
让 bot 基于你的近期战绩进行锐评。
/爱瓦 开一局
让 bot 推演你开的下一局竞技模式。
/爱瓦 PK <群友昵称>
与另一个已绑定者就近期战绩发起 PK。
/爱瓦 昵称 <名字>
设置自己的昵称，不设则默认使用你的 QQ 昵称。
/爱瓦 状态
查看自己的绑定状态。
/爱瓦 解绑
解除绑定并立即删除个人数据。
/爱瓦 帮助
就是这条。
直接用自然语言问也可以，例如「我的无畏契约战绩怎么样？」"""

_NICKNAME_LIMIT = 16

# PK 指令后面跟的名字。`message_str` 在 QQ 官方上已经被适配器剥掉
# `/爱瓦` 前缀和所有 @，剩下的就是「PK 后面那一截」。
_PK_SUFFIX = re.compile(rf"{re.escape(_GROUP)}\s*(?:PK|pk|对战)\s*(.+)", re.IGNORECASE)


def _pk_target_name(body: str) -> str:
    """从指令正文里抠出要比的人的名字。

    「/爱瓦 PK YJJ」→ `YJJ`。抠不出返回空串。
    """
    hit = _PK_SUFFIX.search(str(body or ""))
    if not hit:
        return ""
    return hit.group(1).strip().strip("：:@")[:_NICKNAME_LIMIT]


class UserFacingError(Exception):
    """取不到数据但**不是故障**时抛它，消息原文发给用户。

    跟异常放在一起是因为走返回值得在同一函数里既要 `yield` 又要
    `return <值>`——那在 async generator 里直接 SyntaxError。
    """


class _MissFallbackFilter(CustomFilter):
    """`/爱瓦 <不是子命令>` 时自己接住，别让消息流到模型手里。

    `filter` 的返回值语义是「这条消息我管不管」，**不是**「我有没有命中」——
    `CommandGroupFilter.filter` 里`if not self.custom_filter_ok(...): return False`，
    返回 False 会把整个指令组毙掉。所以这里恒返回 True：命中就自己给结果并
    `stop_event()`，没命中就放行给子指令。写成「命中返回 False」会让
    `/爱瓦 绑定` 这种正常指令完全没反应。
    """

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        text = getattr(event, "message_str", "") or ""
        # 这条过滤器对**每一条消息**都会跑（CommandGroupFilter 先跑
        # custom_filter_ok 再检查组名），所以第一件事是判断关不关我事。
        if not starts_with_group(text, _GROUP):
            return True
        logger.debug(f"[aiva] 指令命中 message_str={text!r}")
        if is_bare_command(text, _GROUP):
            # 只发了「/爱瓦」：回帮助。交给框架会甩一串「参数不足」加各指令的
            # docstring，用户看到的是内部说明，不是能发什么。
            event.set_result(MessageEventResult().message(_HELP_TEXT))
            event.stop_event()
            return True
        if is_command_miss(text, _GROUP):
            event.set_result(MessageEventResult().message(miss_hint(leftover_arg(text, _GROUP))))
            event.stop_event()
        return True


class Main(Star):
    # 别写成 `self.aiva_group.add_custom_filter(...)`——`self.aiva_group` 是
    # `RegisteringCommandable`，没有那个方法，加载时会 AttributeError。
    @custom_filter(_MissFallbackFilter)
    @command_group(_GROUP, alias=_GROUP_ALIASES)
    def aiva_group(self):
        """无畏契约国服战绩查询。指令都在这个组下，不占用通用名字。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        """`config` 必须可省。

        AstrBot 的 `star_manager` 只在 `if plugin_config and ...:` 分支里才传配置；
        仓库没有 `_conf_schema.json` 时那个分支不走，于是只传 context。声明成
        必需参数会 `TypeError: missing 1 required positional argument: 'config'`，
        插件整个加载不起来。
        """
        super().__init__(context)
        self.config = config
        # transport 刻意不 import astrbot（本地 pytest 要能直接跑它），
        # 代价是拿不到框架 logger。这里把 logger 挂给它 ——
        # 「用户说没反应」这类问题唯一的出路就是日志。
        set_log_hook(lambda line: logger.info(f"[aiva] {line}"))
        self._request_timeout = _config_int(config, "request_timeout_seconds", 20)
        self._model_timeout = _config_int(config, "model_timeout_seconds", int(_MODEL_TIMEOUT))
        self._pk_enabled = _config_flag(config, "pk_enabled", True)
        self._store = CredentialStore(self._data_dir())

    def _data_dir(self) -> Path:
        from astrbot.core.star.star_tools import StarTools

        data_dir = StarTools.get_data_dir(_PLUGIN_NAME)
        _migrate_legacy_credentials(data_dir)
        return data_dir

    @aiva_group.command("帮助", alias={"help", "菜单"})
    async def help_cmd(self, event: AstrMessageEvent):
        """列出所有指令。"""
        yield event.plain_result(_HELP_TEXT)

    @aiva_group.command("绑定")
    async def bind(self, event: AstrMessageEvent):
        """QQ 扫码绑定掌上无畏契约账号。"""
        user_id = event.get_sender_id()
        if not user_id:
            yield event.plain_result("拿不到你的用户标识，无法绑定。")
            return

        existing = self._store.read(user_id)
        if existing and is_complete(existing):
            # **别一上来就挡人。** 以前这里无条件回「你已经绑定了，要换号
            # 先解绑」，而战绩/锐评那边过期时又只会说「请重新绑定」——
            # 用户被两边打乒乓球：战绩让他绑定，绑定让他解绑，而没人告诉他
            # 要先解绑。实测踩过（00:19 那轮）。
            #
            # 所以先看这个绑定**还活着吗**，死了就允许直接覆盖重扫。
            alive = await self._binding_alive(user_id, existing)
            if alive:
                yield event.plain_result(
                    f"已经绑定了账号（{masked_id(existing)}），换号先发 /爱瓦 解绑。"
                )
                return
            logger.info(f"[aiva] 用户 {user_id} 已有绑定但已失效，允许直接覆盖重绑")
            yield event.plain_result(f"之前的绑定失效了（{masked_id(existing)}），重新扫码就行。")

        session = QrSession()
        try:
            try:
                qr_image = await session.create_qr()
            except RuntimeError as exc:
                logger.warning(f"[aiva] 出码失败：{exc}")
                yield event.plain_result(f"生成二维码失败（{exc}），稍后再试。")
                return
            except Exception as exc:  # noqa: BLE001
                # aiohttp 的连接错、解析错等等一律翻译成人话。
                # 让它冒到框架手上，用户看到的是「在调用插件 astrbot_plugin_aiva
                # 的处理函数 绑定 时出现异常」加一段 traceback——毫无信息量。
                logger.warning(f"[aiva] 出码异常：{type(exc).__name__}: {exc}")
                yield event.plain_result("生成二维码时网络出错，稍后再试。")
                return

            from astrbot.core.message.components import Image, Plain

            yield event.chain_result(
                [
                    Image.fromBytes(qr_image),
                    Plain("用手机 QQ 扫码，50 秒内有效。"),
                ]
            )

            # 从**出码那一刻**起掐表：二维码的寿命是从出码开始算的，
            # 晚一帧问就少一帧可用时间。
            deadline = time.monotonic() + _POLL_BUDGET
            next_tick = time.monotonic()
            rounds = 0
            while True:
                now = time.monotonic()
                if now >= deadline:
                    logger.info(f"[aiva] 扫码轮询耗尽预算，共问{rounds} 轮")
                    yield event.plain_result("扫码超时，重新发 /爱瓦 绑定。")
                    return

                # 补足到下一个节奏点。网络慢就从预算里扣，不会像原来那样
                # 把轮询周期越拖越长。
                await asyncio.sleep(max(0.0, next_tick - now))
                if time.monotonic() >= deadline:
                    logger.info(f"[aiva] 补觉期间预算耗尽，共问{rounds} 轮")
                    yield event.plain_result("扫码超时，重新发 /爱瓦 绑定。")
                    return

                rounds += 1
                next_tick += _POLL_INTERVAL
                try:
                    result = await session.poll_once()
                except RuntimeError as exc:
                    logger.info(
                        f"[aiva] 第{rounds}轮登录收尾失败：{exc}"
                        f"\n  腾讯原始返回={redact(getattr(session, 'last_raw', '')[:200])!r}"
                        f"\n  跳转链轨迹=\n    "
                        + "\n    ".join(getattr(session, "trace", []) or ["(没走成任何一跳)"])
                    )
                    # 「码过期」和「解析失败」是两回事，用户看到的文案必须分开：
                    # 前者是我们问得太慢，后者是我们自己的解析 bug。
                    if "过期" in str(exc):
                        yield event.plain_result(f"扫码失败：{exc}。码已过期，重新发 /爱瓦 绑定。")
                    else:
                        yield event.plain_result(f"扫码失败：{exc}。")
                    return
                except asyncio.TimeoutError:
                    # 扫码期间网络抖一下不该把整条指令炸掉——那会在群里弹出
                    # 「在调用插件 astrbot_plugin_aiva 的处理函数 绑定 时出现异常」。
                    # 单次失败按「还没扫完」处理，继续轮询。
                    logger.warning(f"[aiva] 第{rounds}轮轮询超时，继续等")
                    continue
                if result:
                    logger.info(f"[aiva] 第{rounds}轮拿到扫码结果")
                    break
                raw_tail = getattr(session, "last_raw", "")[:160]
                logger.debug(f"[aiva] 第{rounds}轮未确认，腾讯返回 {raw_tail!r}")

            client = MvalClient(self._request_timeout)
            try:
                credential = await client.login_by_qq(result.openid, result.access_token)
            except ApiError as exc:
                logger.warning(f"[aiva] 换票失败：{exc}")
                yield event.plain_result(f"登录成功，换取凭证失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[aiva] 换票异常：{type(exc).__name__}: {exc}")
                yield event.plain_result("换取凭证时网络出错，请重试。")
                return

            if not is_complete(credential):
                yield event.plain_result("拿不到完整凭证，请重试。")
                return

            # 昵称是 PK 认人的唯一兜底（QQ 官方收不到 @别人），
            # 每次绑定都刷新一次——用户改了群昵称也能跟上。
            logger.info(f"[aiva] 用户 {user_id} 绑定成功")
            yield event.plain_result(self._save_and_reply(event, user_id, credential))
        finally:
            await session.close()

    @aiva_group.command("绑定微信")
    async def bind_wechat(self, event: AstrMessageEvent):
        """微信扫码绑定掌上无畏契约账号。

        掌瓦的微信登录是「先拿 SDK 票据，再用微信官方接口换二维码，轮询拿
        授权码，最后用授权码换票」——全程 HTTP，不需要浏览器侧车。
        """
        user_id = event.get_sender_id()
        if not user_id:
            yield event.plain_result("拿不到你的用户标识，无法绑定。")
            return

        existing = self._store.read(user_id)
        if existing and is_complete(existing):
            alive = await self._binding_alive(user_id, existing)
            if alive:
                yield event.plain_result(
                    f"已经绑定了账号（{masked_id(existing)}），换号先发 /爱瓦 解绑。"
                )
                return
            logger.info(f"[aiva] 用户 {user_id} 已有绑定但已失效，允许直接覆盖重绑")
            yield event.plain_result(f"之前的绑定失效了（{masked_id(existing)}），重新扫码就行。")

        session = WechatQrSession()
        try:
            try:
                qr_image = await session.create_qr()
            except RuntimeError as exc:
                logger.warning(f"[aiva] 微信出码失败：{exc}")
                yield event.plain_result(f"生成微信二维码失败（{exc}），稍后再试。")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[aiva] 微信出码异常：{type(exc).__name__}: {exc}")
                yield event.plain_result("生成微信二维码时网络出错，稍后再试。")
                return

            from astrbot.core.message.components import Image, Plain

            yield event.chain_result(
                [
                    Image.fromBytes(qr_image),
                    Plain("用微信扫码，50 秒内有效。"),
                ]
            )

            try:
                code = await session.wait_for_code(_POLL_BUDGET)
            except TimeoutError:
                logger.info("[aiva] 微信扫码超时")
                yield event.plain_result("扫码超时，重新发 /爱瓦 绑定微信。")
                return
            except RuntimeError as exc:
                logger.info(f"[aiva] 微信扫码结束：{exc}")
                yield event.plain_result(f"扫码失败：{exc}，重新发 /爱瓦 绑定微信。")
                return
            except asyncio.TimeoutError:
                yield event.plain_result("扫码超时，重新发 /爱瓦 绑定微信。")
                return

            client = MvalClient(self._request_timeout)
            try:
                credential = await client.login_by_wechat(code)
            except ApiError as exc:
                logger.warning(f"[aiva] 微信换票失败：{exc}")
                yield event.plain_result(f"微信登录成功，换取凭证失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[aiva] 微信换票异常：{type(exc).__name__}: {exc}")
                yield event.plain_result("换取凭证时网络出错，请重试。")
                return

            if not is_complete(credential):
                logger.warning(f"[aiva] 微信换票返回的凭据不完整：{sorted(credential)}")
                yield event.plain_result("拿不到完整凭证，请重试。")
                return

            logger.info(f"[aiva] 用户 {user_id} 微信绑定成功")
            yield event.plain_result(self._save_and_reply(event, user_id, credential))
        finally:
            await session.close()

    @aiva_group.command("解绑")
    async def unbind(self, event: AstrMessageEvent):
        """解除绑定并删除本地凭据。"""
        user_id = event.get_sender_id()
        if not self._store.delete(user_id):
            yield event.plain_result("还没绑定。")
            return
        logger.info(f"[aiva] 用户 {user_id} 解绑")
        yield event.plain_result("已解绑。")

    @aiva_group.command("昵称")
    async def set_nick(self, event: AstrMessageEvent, raw_name: GreedyStr):
        """改 PK 时被叫的名字。不带参数时看当前是什么。

        QQ 官方机器人收不到 @别人（实测 `raw_message.mentions` 是空数组），
        所以 PK 只能按名字认人。名字默认取绑定时的平台显示名，而**改名字不该
        逼人重扫一次码**——重绑要走完整套扫码流程，比改个名字重得多。
        """
        user_id = event.get_sender_id()
        credential = self._store.read(user_id)
        if not credential or not is_complete(credential):
            yield event.plain_result("还没绑定，先发 /爱瓦 绑定。")
            return

        # 先洗掉换行与零宽字符：昵称会被拼进 PK 提示词，带换行等于多一行指令。
        new_name = clean_nickname(raw_name)
        if not new_name:
            current = clean_nickname(credential.get("昵称") or "")
            yield event.plain_result(f"当前 PK 名字：「{current or '（还没设）'}」。")
            return

        # 名字要念进 PK 的提示词里，太长会把版面挤掉，也让人在 PK 时敲不完。
        if len(new_name) > _NICKNAME_LIMIT:
            yield event.plain_result(f"名字太长了，最多 {_NICKNAME_LIMIT} 个字。")
            return

        # 重名别人第一反应是「我是不是被占了」，所以顺手告诉他有几个人同名。
        clashes = [name for _, name in find_by_nickname(self._store, new_name, me=user_id)]

        self._store.write(user_id, set_nickname(credential, new_name))
        logger.info(f"[aiva] 用户 {user_id} 昵称改为 {new_name!r}")
        if clashes:
            names = "、".join(clashes)
            yield event.plain_result(
                f"PK 名字改成「{new_name}」，已经有 {len(clashes)} 个人叫这个（{names}）。"
            )
            return
        yield event.plain_result(f"PK 名字改成「{new_name}」。")

    @aiva_group.command("状态")
    async def status_cmd(self, event: AstrMessageEvent):
        """看当前绑定是否正常。"""
        user_id = event.get_sender_id()
        credential = self._store.read(user_id)
        if not credential:
            yield event.plain_result("还没绑定，先发 /爱瓦 绑定。")
            return
        if not is_complete(credential):
            yield event.plain_result(
                f"本地记录不完整（{masked_id(credential)}），重新发 /爱瓦 绑定。"
            )
            return

        # 刚失效过就别急着报好消息：这时候打接口十有八九能撞上一个「好窗口」，
        # 用户看到「绑定正常」转头又被打脸。
        if recently_expired(credential):
            yield event.plain_result(f"已绑定（{masked_id(credential)}），{_LOGIN_EXPIRED_HINT}")
            return

        client = MvalClient(self._request_timeout)
        try:
            role = await client.main_role(credential)
        except LoginExpired:
            yield event.plain_result(f"已绑定（{masked_id(credential)}），{_LOGIN_EXPIRED_HINT}")
            return
        except ApiError as exc:
            yield event.plain_result(f"接口取不到角色信息：{exc}")
            return

        # **状态必须真的打一次战绩接口**。只查主角色是不够的：
        # `get_main_role_raw` 只要 Cookie 形状对就回 result=0，而真正需要
        # 登录态的 `record/list` 早就 1003 了。用户被告知「绑定正常」，
        # 转头发锐评却被告知「登录态失效」——两个都是真话，只是查的不是
        # 同一件事。这是用户实际踩到的问题：状态永远绿着，直到用的时候才炸。
        matches_ok = True
        matches_reason = ""
        try:
            await self._fetch_tracked(user_id, credential, page_size=1)
        except LoginExpired:
            matches_ok = False
            matches_reason = "登录态已失效"
        except ApiError as exc:
            matches_ok = False
            matches_reason = f"战绩接口报错：{exc}"
        except Exception as exc:  # noqa: BLE001
            matches_ok = False
            matches_reason = f"{type(exc).__name__}: {exc}"

        if not matches_ok:
            logger.warning(f"[aiva] 用户 {user_id} 状态自查失败：{matches_reason}")
            yield event.plain_result(
                f"绑定还在（{masked_id(credential)}），但战绩取不到：{matches_reason}"
            )
            return

        name = str(role.get("nickname") or role.get("name") or "").strip()
        line = f"绑定正常（{masked_id(credential)}）"
        if name:
            line = f"绑定正常：{name}（{masked_id(credential)}）"

        seconds = seconds_since_bind(credential)
        if seconds is not None:
            hours = seconds / 3600
            line += f"\n已绑定 {hours:.1f} 小时"

        yield event.plain_result(line)

    @aiva_group.command("战绩")
    async def matches_cmd(self, event: AstrMessageEvent):
        """近期战绩数据。"""
        user_id = event.get_sender_id()
        try:
            data = await self._recent_stats(user_id)
        except UserFacingError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(data[0])

    @llm_tool(name="get_valorant_profile")
    async def profile_tool(self, event: AstrMessageEvent) -> str:
        """读取提问者自己绑定的《无畏契约》国服账号战绩，并给出结构化摘要。

        Returns:
            胜负与胜率、场均 K/D/A、KDA、ACS、回合净胜、最近走势、地图与
            常用特工等统计结果。未绑定或登录态失效时返回说明文字。
        """
        try:
            return (await self._recent_stats(event.get_sender_id()))[0]
        except UserFacingError as exc:
            return str(exc)

    @aiva_group.command("锐评")
    async def review_cmd(self, event: AstrMessageEvent):
        """让模型基于近期数据写一段锐评。"""
        user_id = event.get_sender_id()
        try:
            data = await self._recent_stats(user_id)
        except UserFacingError as exc:
            yield event.plain_result(str(exc))
            return

        text = data[0]
        try:
            result = await self._ask_model(event, review_prompt(text))
        except Exception as exc:
            logger.warning(f"[aiva] 锐评调用模型失败：{type(exc).__name__}: {exc}")
            yield event.plain_result(f"调用模型失败：{exc}")
            return
        if not result:
            yield event.plain_result(f"模型没有返回内容。先把数据给你：\n\n{text}")
            return
        yield event.plain_result(result)

    @aiva_group.command("开一局", alias={"开局", "来一局"})
    async def play_match(self, event: AstrMessageEvent):
        """随机生成一局竞技模式，让模型推演过程与结果。"""
        user_id = event.get_sender_id()
        credential = self._store.read(user_id)
        summary: dict[str, Any] | None = None

        if credential and is_complete(credential):
            try:
                raw_list, _role = await self._fetch_tracked(user_id, credential)
                summary = aggregate(raw_list)
            except Exception as exc:  # noqa: BLE001 - 推演不因查不到就停
                logger.info(f"[aiva] 开一局读战绩失败，按默认水平：{exc}")

        rng = random.Random()
        setup = build_match_setup(rng, summary if summary and summary.get("场数") else None)
        try:
            result = await self._ask_model(event, match_prompt(setup_text(setup)))
        except Exception as exc:
            logger.warning(f"[aiva] 开一局调用模型失败：{type(exc).__name__}: {exc}")
            yield event.plain_result(f"调用模型失败：{exc}")
            return
        if not result:
            yield event.plain_result(f"模型没有返回内容。这局的设定：\n\n{setup_text(setup)}")
            return
        yield event.plain_result(result)

    @aiva_group.command("PK", alias={"pk", "对战"})
    async def pk_cmd(self, event: AstrMessageEvent):
        """和另一个已绑定的人比近期战绩。"""
        if not self._pk_enabled:
            yield event.plain_result("PK 已关闭。")
            return

        me = event.get_sender_id()
        my_cred = self._store.read(me)
        if not my_cred or not is_complete(my_cred):
            yield event.plain_result("还没绑定，先发 /爱瓦 绑定。")
            return

        message_obj = getattr(event, "message_obj", None)
        bot_id = str(getattr(event, "self_id", "") or "")
        raw = getattr(message_obj, "raw_message", None)
        text_body = str(getattr(event, "message_str", "") or "")

        opponent_id = opponent_from_raw(raw, me=me, bot_id=bot_id)
        source = "原始报文"
        opponent_nick = ""
        if not opponent_id:
            opponent_id = opponent_from_mentions(raw, me=me, bot_id=bot_id)
            source = "提及列表"
        if not opponent_id:
            opponent_id = find_opponent(
                getattr(message_obj, "message", None) or [],
                text_body,
                me=me,
                bot_id=bot_id,
            )
            source = "组件链/正文"
        if not opponent_id:
            keyword = _pk_target_name(text_body)
            candidates = find_by_nickname(self._store, keyword, me=me) if keyword else []
            if len(candidates) == 1:
                opponent_id, opponent_nick = candidates[0]
                source = "昵称"
            elif len(candidates) > 1:
                yield event.plain_result(
                    f"有 {len(candidates)} 个人叫「{keyword}」，写完整一点：\n"
                    + "\n".join(f"· {name}" for _, name in candidates)
                )
                return
            elif keyword:
                yield event.plain_result(f"没找到叫「{keyword}」的人，对方得先绑定并设昵称。")
                return

        # 认不出对手时把拿到的东西记下来。QQ 官方机器人**收不到 @别人**：
        # `raw_message.mentions` 实测是 `[]`，平台压根没把被 @ 的人传下来。
        # 只记能定位问题的字段，不落消息正文与原始报文内容。
        summary = mention_summary(raw)
        logger.info(
            f"[aiva] PK 找对手：自己={me!r} 认出={opponent_id!r} 来源={source} 提及={summary!r}"
        )
        if not opponent_id:
            yield event.plain_result("没认出要和谁比，写对方的名字：/爱瓦 PK <名字>。")
            return

        rival_cred = self._store.read(opponent_id)
        if not rival_cred or not is_complete(rival_cred):
            yield event.plain_result("对方还没绑定，比不了。")
            return

        # 念名字优先用各人自己设的 PK 名字，那是群里点人时用的称呼。
        my_name = self._versus_name(my_cred, self._display_name(event), "你")
        rival_name = self._versus_name(rival_cred, opponent_nick, f"对手{masked_id(rival_cred)}")

        # 两边分开取，失败时要说清是谁的登录态挂了——只挂一边却让两个人
        # 都去重扫，一半的人白扫一次。合成一个 try 是做不到的：第一句抛异常
        # 时 `my_list` 还没绑上，except 里再去读它只会 UnboundLocalError。
        try:
            my_list, my_role = await self._fetch_tracked(me, my_cred)
        except LoginExpired:
            yield event.plain_result(f"{my_name}的登录态失效，重新发 /爱瓦 绑定。")
            return
        except ApiError as exc:
            yield event.plain_result(f"接口不给数据：{exc}")
            return
        except Exception as exc:
            logger.warning(f"[aiva] PK 取自己战绩失败：{type(exc).__name__}: {exc}")
            yield event.plain_result(f"读取你的战绩失败：{exc}")
            return

        try:
            rival_list, rival_role = await self._fetch_tracked(opponent_id, rival_cred)
        except LoginExpired:
            yield event.plain_result(f"{rival_name}的登录态失效，让他重发 /爱瓦 绑定。")
            return
        except ApiError as exc:
            yield event.plain_result(f"接口不给数据：{exc}")
            return
        except Exception as exc:
            logger.warning(f"[aiva] PK 取对手战绩失败：{type(exc).__name__}: {exc}")
            yield event.plain_result(f"读取对方的战绩失败：{exc}")
            return

        my_summary = aggregate(my_list)
        rival_summary = aggregate(rival_list)
        if not my_summary.get("场数") or not rival_summary.get("场数"):
            yield event.plain_result("有一边没有可统计的对局，比不了。")
            return

        points = contrast_points(my_summary, rival_summary)
        if not points:
            yield event.plain_result("两边没有可比的项。")
            return

        try:
            result = await self._ask_model(
                event,
                pk_prompt(
                    my_name,
                    rival_name,
                    contrast_text(points, my_name, rival_name),
                    advantage_count(points, my_name, rival_name),
                    recent_text(my_summary, tier_of(my_role)),
                    recent_text(rival_summary, tier_of(rival_role)),
                ),
            )
        except Exception as exc:
            logger.warning(f"[aiva] PK 调用模型失败：{type(exc).__name__}: {exc}")
            yield event.plain_result(f"调用模型失败：{exc}")
            return

        yield event.plain_result(result or "模型没有返回内容。")

    def _display_name(self, event: AstrMessageEvent) -> str:
        """发起指令那个人的平台显示名。太长的裁掉，PK 里要念出来。

        **故意不是 async**：函数体里没有任何 await，做成协程只会引来
        一类事故——调用点漏写 `await` 时不报错，而是拿到一个协程对象，
        它是真值（`or "你"` 救不了），还会被 f-string 友好地渲染成
        `<coroutine object Main.名字 at 0x7fa...>`，一路喂进提示词。
        线上真出过：模型把这段字符串当成玩家名字，一本正经点评了半天
        「`<coroutine object>`的9.4场均死亡就是铁证」。
        同步函数漏写 await 的地方根本调不出结果，TypeError 当场就炸。
        """
        try:
            name = clean_nickname(event.get_sender_name() or "")
        except Exception:
            return ""
        return name[:_NICKNAME_LIMIT]

    @staticmethod
    def _versus_name(credential: dict[str, Any], platform_name: str, fallback: str) -> str:
        """PK 时念出来的名字。

        优先用他自己在 `/爱瓦 昵称` 里设的名字——那是群里点人时用的称呼，
        而平台给的显示名在 QQ 官方上是 QQ 昵称而不是群昵称，常常点不到人。
        """
        # 老记录可能是净化之前存进去的，念之前再洗一次。
        return (
            clean_nickname(credential.get("昵称") or "")
            or clean_nickname(platform_name)
            or fallback
        )

    def _save_and_reply(
        self, event: AstrMessageEvent, user_id: str, credential: dict[str, Any]
    ) -> str:
        """存下凭据，返回给用户看的那句话。QQ 与微信两条绑定路径共用。

        昵称是 PK 认人的唯一兜底（QQ 官方收不到 @别人），每次绑定都刷新一次，
        用户改了群昵称也能跟上。
        """
        self._store.write(
            user_id, set_nickname(stamp_bound_at(credential), self._display_name(event))
        )
        name = str(credential.get("昵称") or "").strip()
        return (
            f"绑定成功（{masked_id(credential)}）。\n"
            f"PK 名字是「{name or '（没取到）'}」，要改发 /爱瓦 昵称。"
        )

    async def _binding_alive(self, user_id: str, credential: dict[str, Any]) -> bool:
        """这份绑定的登录态还能不能取到战绩。

        **必须真的打一次接口**。`get_main_role_raw` 只要 Cookie 形状对就回
        `result=0`，过期了照样回 0——拿它判断等于没判断（状态指令当初就是
        这么给出假绿灯的）。只有 `record/list` 会真的拒绝过期凭据。

        网络抖动拿不到时按「活着」处理：绑定指令只是决定要不要挡人，
        宁可放行让人重扫，也别因为一次抖动把人卡在「你得先解绑」上。
        """
        if not is_complete(credential):
            return False
        if recently_expired(credential):
            # 刚失败过就别拦着重绑——用户要的是直接重扫，不是先解绑。
            return False
        try:
            await self._fetch_tracked(user_id, credential, page_size=1)
        except (LoginExpired, ApiError):
            return False
        except Exception:  # noqa: BLE001
            return True
        return True

    async def _fetch_tracked(
        self, user_id: str, credential: dict[str, Any], page_size: int = 20
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """取对局，顺手把「刚失效过」记在凭据上。

        只判单次请求的成败是不够的：重绑后的头几分钟，新旧会话在服务端交替
        生效，同一个号会几秒内时好时坏。状态指令如果只看这一次成功就说
        「绑定正常」，用户拿到的是一条随时会翻车的绿线。
        """
        try:
            result = await fetch_matches(credential, page_size=page_size)
        except LoginExpired:
            self._store.write(user_id, mark_expired(credential))
            raise
        if credential.get("最近失效"):
            self._store.write(user_id, clear_expired(credential))
        return result

    async def _recent_stats(self, user_id: str) -> tuple[str, dict[str, Any]]:
        """取数据并折成文本。取不到时抛 `UserFacingError`。

        写成 `return None` 是不行的：调用它的指令本身是异步生成器，而这个辅助
        函数也跟着写了 `yield`——在同一函数里既要 `yield` 又要 `return <值>`，
        Python 直接给 SyntaxError（`return with value in async generator`）。
        真正的加载阶段才报错，本地测试看不到。所以这里一律抛，
        调用方负责把话发出去。
        """
        credential = self._store.read(user_id)
        if not credential or not is_complete(credential):
            raise UserFacingError("还没绑定，先发 /爱瓦 绑定。")

        try:
            raw_list, role = await self._fetch_tracked(user_id, credential)
        except LoginExpired as exc:
            # 文案必须说清「怎么修」：光说「请重新绑定」不够——绑定指令
            # 以前会回「你已经绑定了，要换号先解绑」，用户被两面挡。
            # 现在绑定指令已允许覆盖重扫，这里就照实说「直接重扫」。
            raise UserFacingError(_LOGIN_EXPIRED_HINT) from exc
        except ApiError as exc:
            raise UserFacingError(f"接口不给数据：{exc}") from exc
        except Exception as exc:
            logger.warning(f"[aiva] 取对局列表失败：{type(exc).__name__}: {exc}")
            raise UserFacingError(f"取战绩失败：{exc}") from exc

        summary = aggregate(raw_list)
        if not summary.get("场数"):
            raise UserFacingError("没有可统计的对局。")
        return recent_text(summary, tier_of(role)), summary

    async def _ask_model(self, event: AstrMessageEvent, prompt: str) -> str:
        """用当前会话的模型问一句话，返回它写的内容。

        **直连 provider，不走 `tool_loop_agent`。**

        线上实测：走 `tool_loop_agent` 时锐评稳定卡死 90 秒以上，日志里连
        一行上游请求都看不到；而同一个 key、同一个模型直连
        `/chat/completions` 只要 12 秒就回来了。所以问题不在上游、也不在
        提示长度（实测提示 927 字）。

        `tool_loop_agent` 是给「模型要调工具」的场景用的：它会把当前会话
        挂着的**全部**工具 schema 塞进请求（这台机器装了十几个插件，
        bilibili 一个就注册了 3 个 LLM tool），再走 runner 循环。而锐评、
        开局、PK 全都是「拿一段数据换一段文字」，一个工具都不需要。

        两处必须收紧：
        1. 超时。底层是 HTTP，上游挂起时不会自己回来。实测锐评挂过 2 分
           43 秒、日志一行没有——用户只看到「机器人不理人了」。
        2. 重试。agent_runner 配了 `request_max_retries: 5`，叠满轻松超过
           两分钟，所以这里显式压到 1 次。
        """
        try:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception as exc:
            raise RuntimeError(f"取不到当前对话用的模型：{exc}") from exc

        try:
            provider = await self.context.provider_manager.get_provider_by_id(provider_id)
        except Exception as exc:
            raise RuntimeError(f"取不到模型实例 {provider_id!r}：{exc}") from exc
        if provider is None:
            raise RuntimeError(f"模型 {provider_id!r} 不存在或没启用。")

        logger.info(
            f"[aiva] 调模型 provider={provider_id!r} 提示{len(prompt)}字，"
            f"最多等 {self._model_timeout}秒"
        )

        async def call_once() -> str:
            result = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=_MODEL_SYSTEM_PROMPT,
                    request_max_retries=1,
                ),
                timeout=self._model_timeout,
            )
            return (getattr(result, "completion_text", "") or "").strip()

        # **超时后换新请求重试一次**。挂起的连接不会自己恢复——23:52 的
        # PK 实测：同样 2400 字的提示，直连压测 17 秒返回，这次却挂满
        # 90 秒。上游偶发抽风，唯一有效动作是放弃这条连接重开一条。
        # 重试用同样的超时，最坏 2×90=180 秒，仍远好于无限等。
        started = time.monotonic()
        try:
            text = await call_once()
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            logger.warning(
                f"[aiva] 调模型第1次超过 {self._model_timeout}秒（实际{elapsed:.0f}秒），"
                f"换新请求重试，provider={provider_id!r}"
            )
            retry_started = time.monotonic()
            try:
                text = await call_once()
                logger.info(f"[aiva] 重试用时{time.monotonic() - retry_started:.1f}秒成功")
            except asyncio.TimeoutError:
                total = time.monotonic() - started
                logger.warning(
                    f"[aiva] 调模型两次共 {total:.0f}秒都没回话，放弃，provider={provider_id!r}"
                )
                raise RuntimeError(f"模型 {total:.0f} 秒没回话，稍后再试。") from None

        elapsed = time.monotonic() - started
        logger.info(f"[aiva] 模型用时{elapsed:.1f}秒，返回 {len(text)} 字")
        if not text:
            raise RuntimeError(f"模型返回了空内容，用时 {elapsed:.1f} 秒。")
        return text
