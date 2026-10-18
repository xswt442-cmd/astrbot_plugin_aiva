"""对局列表必须带 scene 与 game_role_id。

线上事故（2026-10-13 19:0x）：绑定成功、登录态正常，但查战绩返回
「登录态失效」。用真凭据打接口才发现：

- `get_main_role_raw` → `result:0`，返回里带着 `scene`(219 字)与 `game_role_id`
- `career/record/list`（不带这两个）→ `result:1003 / "wrong scene: "`
- 同样端点带上这两个 → `result:0`，返回真实对局

而 `_是登录问题` 把 1003 判成登录过期，于是让用户重新扫码——
重扫一百次也没用，锅还背错。

这些测试不碰网络，只钉住「参数必须带」和「1003 不能当登录过期」两件事。
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiva.transport import _是登录问题, _翻译错误, _选角色

源码 = Path(__file__).resolve().parent.parent / "aiva" / "transport.py"
主源码 = Path(__file__).resolve().parent.parent / "main.py"


class Test对局列表必带scene:
    def test_取对局列表要求两个参数(self):
        树 = ast.parse(源码.read_text(encoding="utf-8"))
        for 节点 in ast.walk(树):
            if isinstance(节点, ast.AsyncFunctionDef) and 节点.name == "取对局列表":
                参数 = [a.arg for a in 节点.args.args]
                assert "场景" in 参数 and "角色ID" in 参数, f"签名缺参数：{参数}"
                return
        raise AssertionError("找不到 取对局列表")

    def test_请求体带scene与game_role_id(self):
        文本 = 源码.read_text(encoding="utf-8")
        assert '"game_role_id": 角色ID' in 文本
        assert '"scene": 场景' in 文本

    def test_取对局会自己先取scene(self):
        """调用方不该自己拼这个请求体——少传一个就全盘皆输。"""
        文本 = 源码.read_text(encoding="utf-8")
        assert "取角色上下文" in 文本
        assert "async def 取对局(" in 文本

    def test_调用方都走取对局(self):
        """main.py 里不该再有裸调 取对局列表 的地方。

        正常路径都过 `_取对局记账`：它在 `取对局` 外面记一次成败，
        状态和绑定靠这个记录判断，不能只看单次请求。
        """
        文本 = 主源码.read_text(encoding="utf-8")
        assert "传输.取对局列表(" not in 文本, "调用方必须用 取对局()，它会自己取 scene"
        直接 = 文本.count("await 取对局(")
        记账 = 文本.count("_取对局记账(")
        assert 直接 + 记账 >= 5, "四个业务调用点都该走 取对局/取对局记账"
        assert 直接 == 1, "只有 _取对局记账 内部该裸调 取对局，别处一律走记账版"


class Test1003不是登录过期:
    def test_wrong_scene不算登录问题(self):
        原始 = {"result": 1003, "err_msg": "wrong scene: "}
        assert not _是登录问题(原始), "参数错被当成登录过期，会让用户白扫一百次码"

    def test_wrong_scene有自己的文案(self):
        文案 = _翻译错误({"result": 1003, "err_msg": "wrong scene: "})
        assert "重扫码" not in 文案
        assert "bug" in 文案 or "缺参数" in 文案

    def test_真正的登录过期仍然要判对(self):
        原始 = {"result": 999999, "err_msg": "auth web ticket fail DecodeTgpTicket"}
        assert _是登录问题(原始)

    def test_裸1003没消息时按可疑处理(self):
        """没有消息可依据时宁可怀疑登录态，也不要直接放行。"""
        assert _是登录问题({"result": 1003})

    def test_login关键词也算(self):
        assert _是登录问题({"result": -1, "err_msg": "please login again"})


class Test挑对游戏角色:
    """同一账号下「无畏契约」和「无畏契约手游」两个角色都会返回，顺序不固定。

    实测（2026-10-19 00:44）：拿手游角色去查战绩，接口回
    `result=1003 err_msg='invalid scene game_id'`，被当成登录失效，
    用户白重绑了三次。端游角色才是要查的那个。
    """

    端游 = {"gameName": "无畏契约", "game_role_id": "651cca5e", "tier_text": "白银II"}
    手游 = {"gameName": "无畏契约手游", "game_role_id": "24824969", "tier_text": "青铜II"}

    def test_手游排在前面也选端游(self):
        assert _选角色([self.手游, self.端游]) is self.端游

    def test_端游排在前面不用挑(self):
        assert _选角色([self.端游, self.手游]) is self.端游

    def test_没有端游角色就退回第一个(self):
        assert _选角色([self.手游]) is self.手游

    def test_空列表返回空(self):
        assert _选角色([]) == {}

    def test_取角色上下文不取死第一个(self):
        源 = 源码.read_text(encoding="utf-8")
        体 = 源[源.index("async def 取角色上下文") :]
        体 = 体[: 体.index("async def 取对局列表")]
        assert "_选角色(" in 体, "取 list[0] 会随机撞上手游角色"

    def test_invalid_scene不算登录过期(self):
        """查错了角色（手游 vs 端游）也会回 1003，这个不能让用户重扫码。"""
        原始 = {"result": 1003, "err_msg": "invalid scene game_id"}
        assert not _是登录问题(原始)
        assert "角色" in _翻译错误(原始)
