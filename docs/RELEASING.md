# 发版

一次发版是升一个版本号、合并到 `main`，然后在 Actions 里触发 `release`
工作流，它会打标签并建 Release。

**不要本地打标签。** 标签和 Release 是两个对象，分开手做会漏。爱农那边
v0.1.2 与 v0.1.3 就只推了标签，Releases 页面停在 v0.1.1。

分支推送不发布任何东西。示例用 `dev`，换别的开发分支同理。

## 一次发版会影响什么

- **GitHub Release** —— 手动触发 `release` 工作流，不自动。
- **AstrBot 插件市场** —— **不跟 GitHub Release 走，要手工提交**。要在
  <https://cloud.astrbot.app/publish> 填本仓库地址，注册 AstrBot Cloud 账号后
  由平台自己抓 `main`、解析 `metadata.yaml`、打包并写进索引。
  **每个版本都要重做一次**，否则用户看到的和装到的都是上一个版本。

  平台会校验压缩包体积**不超过 16 MB**，超了直接拒绝发布。

  两条约定：

  - **文件名必须是 `logo.png`。** 这由 AstrBot 硬编码（`star_manager.py` 里的
    `logo_fname`），改名就不显示。
  - **发版必须合到 `main`。** 平台从 GitHub 仓库取包，`dev` 分支上的东西
    不会被打进 zip。

## 清单

1. 在开发分支上定 `X.Y.Z`。**版本号有三处，一起改**：

   | 文件 | 形式 | 谁读 |
   | --- | --- | --- |
   | `metadata.yaml` | `v0.2.0`（**带 `v` 前缀**） | 插件市场 |
   | `pyproject.toml` | `0.2.0`（不带前缀） | 本地构建与打包 |
   | `uv.lock` | `0.2.0`（`uv lock` 生成） | 锁定依赖 |

   只改 `metadata.yaml` 就能过本地 `pytest` 之外的检查，但**推不上去**——
   `test/test_metadata.py::test_三处版本号一致` 会在 pre-push 钩子里拦下。
   改完 `pyproject.toml` 记得跑 `uv lock` 让 lock 跟上。

   这三处曾经漏过两处，是 pre-push 钩子拦下来的，不是人发现的。

   另外 `docs/CHANGELOG.md` 的首个小节标题要写成 `## [X.Y.Z] - YYYY-MM-DD`，
   工作流会按这个格式去取 Release 说明。

2. 跑本仓库的检查（命令见 `AGENTS.md` 的验证一节）：

   ```sh
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   ```

3. 提交并推开发分支：

   ```sh
   git commit -am "chore: release X.Y.Z"
   git push origin dev
   ```

4. 等这个分支的 CI 变绿。此时**什么都没发布**——标签不存在，CI 只验证。

5. 合并到 `main` 并推送：

   ```sh
   git switch main
   git pull
   git merge --no-ff dev -m "merge: dev -> main"
   git push origin main
   ```

6. 在 Actions 里触发 `release` 工作流：<https://github.com/xswt442-cmd/astrbot_plugin_aiva/actions/workflows/release.yml>
   → Run workflow → 填版本号（带 `v`，如 `v0.2.2`）。

   工作流会自己完成剩下的事，并在每一步前先检查：

   - 触发的是 `main` 而不是别的分支
   - 版本号格式是 `vX.Y.Z`
   - `metadata.yaml`、`pyproject.toml`、`docs/CHANGELOG.md` 三处版本号一致
   - `main` 的 CI 是绿的（最多等 10 分钟）
   - 这个标签还没被占用
   - `git archive` 出来的包里没有 `test/` 和 `.github/`，有 `logo.png`，且不超过 16 MB

   然后打标签（显式写 `main`）、建 Release，说明自动取 `CHANGELOG` 对应小节。
   最后核对最新的 Release 是 `Latest` 且版本号正确。

   工作流跑完后，**市场发布还没做**，继续第 7 步。

7. 去 AstrBot 插件市场提交 —— **每个版本都要做一次**

   开 <https://cloud.astrbot.app/publish>，填本仓库地址提交：
   `https://github.com/xswt442-cmd/astrbot_plugin_aiva`

   **市场不跟 GitHub Release 走，没有自动同步。** 没做这一步，用户 WebUI 里
   一直看到上一个版本，装到的也是旧代码——GitHub 上 tag 和 Release 明明都是新的。

## 兼容性

`astrbot_version` 是插件声明支持的 AstrBot 版本区间，填错了市场会显示不兼容。
当前声明 `>=4.16,<5`，用到的 API（`command_group`、`llm_tool`、`tool_loop_agent`）
都是 4.x 引入的。升到 AstrBot 5 时先确认这些 API 有没有变。

`requirements.txt` 是**手写**的，不要用 `uv export` 生成。原因：

- `uv export` 会把每个包钉成 `==`。AstrBot 装插件时把这份清单喂给 pip，
  而 pip 装在与核心同一的环境里；核心给这些包装了版本护栏，插件若要求精确
  版本，护栏会拦下 pip，安装整体失败。
- 它还会把传递依赖全部展开写进去，而插件只需要声明直接依赖。

加了依赖时**两边一起改**：`pyproject.toml` 的 `dependencies` 与
`requirements.txt`。约束由 `test/test_requirements.py` 钉住，跑测试就能发现
漏改。

`requirements.txt` 的版本下界要与 AstrBot 核心的声明对齐（当前 `aiohttp` 按
核心写 `>=3.11.18`），比 `pyproject.toml` 更严是可以的，更松会让 pip 去动核心
的包。

## 图标

`logo.png` 在仓库根目录，文件名与位置都是 AstrBot 硬编码的（`star_manager.py`
里的 `logo_fname`），不要另建目录。WebUI 通过一个 300 秒有效的文件 token 拉图，
所以体积要小——超过几百 KB 每次刷新都会重传。

规格是 256×256、RGB 无 alpha。

**换图标直接替换根目录的 `logo.png`。** 源图不再进仓库；要重新生成的话，把源图
放回 `icon/aiva-icon.png` 再跑 `python test/做图标.py`——与 aiwm 那份不同，这张图
**不做亮度抠底**（图案暗部与背景亮度重叠，按亮度抠会把人抠没），徽章外圈直径
约占画布 87%，圆形裁切切不到图案，直接整张 LANCZOS 缩放即可。脚本里带一个
自检，图案贴边时会拒绝生成。

`test/做图标.py` 需要 Pillow，它不在 `requirements.txt` 里——那是给 AstrBot
装的运行时清单，而做图标是手工操作。

要看发布包会带哪些文件：

```sh
git archive --format=tar HEAD | tar -tf -
```

## 市场分类

`category` 填市场自己定的那几个中文，「战绩查询」这类归「娱乐」。填错不影响
安装，只是插件详情页的分类标签不对。
