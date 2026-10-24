# Agent guide

`aiva`：通过掌上无畏契约（掌瓦）私有接口读取《无畏契约》国服战绩，交给模型做锐评。

文档规范见 [.github/engineering/STANDARD.md](.github/engineering/STANDARD.md)。

## 约定

- 包内一律相对导入。插件目录被 AstrBot 当作包加载，顶层包名在容器里 import 不到。
- 指令全在 `爱瓦` 组下（`/爱瓦 战绩`），不占通用名。改动指令时帮助文本中的示例同步修改。
- `requirements.txt` **手写**，理由见 `docs/RELEASING.md`。
- 网络请求只用 `aiohttp`。
- 数据落在 `data/plugin_data/aiva/`，不写插件目录。
- 凭据不进日志、不进异常消息、不进提交。解绑时删除文件。`aiva/storage.py` 的 `凭据摘要` 是供日志使用的裁剪版，`扫码结果.__repr__` 也不含本体。
- 地图名与特工名的中文表取自 Riot 官方 `valorant-api.com`（`/v1/maps`、`/v1/agents`，带 `?language=zh-CN`）。**不凭记忆修改**：曾凭记忆写入，其中四个错误、一个为虚构。
- 掌瓦会返回 `used_map.name` 与 `hero_name` 的本地化名称，有真实数据时优先用接口返回值，常量表仅在无历史数据时兜底。

## 结构

- `main.py` — 插件入口：指令组、指令、落空兜底、`查询玩家画像` 这个 LLM 工具
- `aiva/auth/` — 掌瓦扫码登录（纯 HTTP，无浏览器侧车）
- `aiva/transport.py` — 接口传输层：请求头、业务码、登录过期
- `aiva/stats.py` — 对局聚合与文本汇总、PK 逐项对比
- `aiva/match.py` — 「开一局」的对局设定：随机数与水平适配
- `aiva/prompts.py` — 锐评、开一局、PK 的提示词（独立成模块以便单测）
- `aiva/pk.py` — 从原始报文、mentions、组件链与正文中定位被 @ 者
- `aiva/兜底.py` — 指令落空的判据与提示
- `aiva/storage.py` — 凭据与绑定关系
- `test/` — pytest 用例与手工验证脚本
- `docs/` — 更新日志与术语表

## 指令落空与组别名

`/爱瓦 <未匹配子命令的内容>` 时指令被跳过，消息进模型，模型会自行作答。指令组挂 `_落空兜底`（`CustomFilter`）：仅在落空时 `set_result` + `stop_event`，判据在 `aiva/兜底.是否落空`。

两处易写反的逻辑，`test_main_wiring.py` 均已覆盖：

- `.filter` 返回值的语义是「本组是否处理这条消息」，不是「是否命中」——**所有出口必须 `return True`**，写成「命中返回 False」会使整个指令组失效；
- `取落空参数` 对不属于本组的消息必须返回**空串**，返回原文本会把其他插件的指令判为本组落空，`stop_event()` 执行后该插件无响应。

组名另挂 `_组别名 = {f"/{_组名}"}`：QQ 官方的 At 唤醒分支不剥 `message_str` 里的 `/`，缺少该别名时 `@机器人 /爱瓦 战绩` 无响应。

## 数据边界（写提示词时必须知道）

掌瓦对局列表**不含** ADR、KAST、经济、攻防回合、命中率分布、武器明细、队友信息。`aiva/prompts.py` 的 `字段白名单` 把这份清单写入提示词，用于避免模型套用 CS2 指标自行构造答案。

同理，新增统计维度时**先改 `stats.py` 取得真实字段**，不先在提示词中向模型承诺数据。

## config 必须可省

AstrBot 的 `star_manager` 这样注入配置：

```python
if plugin_config and metadata.star_cls_type:
    star = metadata.star_cls_type(context=context, config=plugin_config)
else:
    star = metadata.star_cls_type(context=context)
```

仓库没有 `_conf_schema.json` 时第一个分支不执行，只传 `context`。`__init__` 中把 `config` 声明为必需参数会抛出 `TypeError`，插件无法加载。

## 模型调用：两条路径

`@llm_tool(name="get_valorant_profile")` 的 `查询玩家画像` 走 AstrBot 工具链路——用户直接询问战绩时由模型自行调用。

锐评、开一局、PK 不走该链路：`_问模型` 直连 provider 的 `text_chat()`，**不用 `tool_loop_agent`**。实测后者稳定卡死 90 秒以上（日志中无上游请求记录），同一 key 直连耗时 12 秒；原因是 `tool_loop_agent` 会把当前会话挂载的全部工具 schema 加入请求并走 runner 循环，而这三条指令不需要工具。此处另设超时（`_模型超时`）与重试（超时后换新连接重试一次）。

## 登录链路的三条实测结论

改动 `aiva/auth/ptlogin.py` 前先过一遍：

1. 出码必须请求 `xui.ptlogin2.qq.com/ssl/ptqrshow`。换成 `ssl.ptlogin2.qq.com` 返回 HTML 而非 PNG——同一路径，只有 `xui.` 子域出图。
2. `xlogin` 下发的 `pt_login_sig` 必须用**同一个 cookie jar** 带回。新建 Session 会丢失，轮询必然失败。因此 `扫码会话` 是长生命周期对象，用完必须 `await 关闭()`。
3. 掌瓦链路**纯 HTTP，不需要浏览器侧车**（与爱农的营地链路不同，后者需要 YSDK 签名）。

## 接口会失效

`app.mval.qq.com` 是私有接口，`mcode` 硬编码。腾讯轮换时所有请求同时失败，表现为 `业务错误：接口返回错误码 N`。此时需要重新抓包，不继续调整提示词。

`career/mode/filter` 是 **GET** 接口。用 POST 访问返回 404，这只是请求方式不匹配——换成 GET 正常返回 999999（接口存活，需登录）。

改动或发布前先运行 `test/verify_mval.py` 探测接口；改动 `aiva/stats.py` 的字段映射前，用真实登录态运行一次并核对字段名。
