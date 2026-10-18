"""绑定期过了以后，用户不该被「战绩 → 绑定 → 解绑」踢来踢去。

线上真事（00:19 那轮）：

```
战绩 → 「登录态失效了，请重新 /爱瓦 绑定。」
绑定 → 「你已经绑定了账号。要换号先用 /爱瓦 解绑。」
```

用户只是想续期，却被两边打乒乓球：战绩让他绑定，绑定让他解绑，
而**没人告诉他要先解绑**。这是设计出来的死循环，不是用户的错。

修法：绑定指令先真打一次战绩接口，死了就允许直接覆盖重扫。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

仓库根 = Path(__file__).resolve().parent.parent
主源码 = (仓库根 / "main.py").read_text(encoding="utf-8")


def _函数体(名: str) -> str:
    树 = ast.parse(主源码)
    节点 = None
    for n in ast.walk(树):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 名:
            节点 = n
    assert 节点 is not None, f"main.py 里找不到 {名}"
    片段 = ast.get_source_segment(主源码, 节点) or ""
    文档 = ast.get_docstring(节点, clean=False)
    return 片段.replace(文档, "") if 文档 else 片段


class Test绑定不再挡人:
    def test_绑定要先查绑定是否还活着(self):
        体 = _函数体("绑定")
        assert "_绑定还活着(" in 体, "绑定指令不能无条件挡已绑定的用户——他可能只是登录态过期了"

    def test_失效时直接让人重扫(self):
        体 = _函数体("绑定")
        assert "重新扫码就行" in 体, "失效时要直接给出重扫这个动作"

    def test_活着时才挡(self):
        """换号才需要解绑。登录态过期不属于「换号」。"""
        体 = _函数体("绑定")
        assert "换号先发 /爱瓦 解绑" in 体


class Test绑定还活着_必须真打接口:
    def test_不能只查主角色(self):
        """`get_main_role_raw` 过期了也回 result=0，拿它判断等于没判断。

        状态指令当初就是这么给出假绿灯的（见 test_状态同步与调模型.py）。
        """
        体 = _函数体("_绑定还活着")
        assert "取对局记账(" in 体, "必须打 record/list，那个才真的拒绝过期凭据"

    def test_登录过期和业务错误都算死了(self):
        体 = _函数体("_绑定还活着")
        assert "登录过期" in 体
        assert "业务错误" in 体

    def test_网络抖动按活着处理(self):
        """绑定指令只决定要不要挡人。抖动时宁可放行让人重扫，
        也别把人卡在「你得先解绑」。"""
        体 = _函数体("_绑定还活着")
        索引 = 体.index("except Exception")
        assert "return True" in 体[索引:], "网络异常必须按「活着」处理，否则抖动会把人挡住"

    def test_不齐全直接算死(self):
        体 = _函数体("_绑定还活着")
        assert "凭据是否齐全" in 体


class Test过期文案要给出动作:
    def test_常量本身带原因与动作(self):
        """票据有固定有效期，只说「重新绑定」等于没说。"""
        源码 = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")

        assert "重新发 /爱瓦 绑定" in 源码
        assert "三天" in 源码

    def test_战绩文案(self):
        体 = _函数体("_取近期战绩")
        assert "_登录过期提示" in 体, "要说清原因，别写死一句「登录态失效」"

    def test_状态文案(self):
        体 = _函数体("状态")
        assert "_登录过期提示" in 体

    def test_PK要指出是谁过期(self):
        """以前只说「有人的登录态失效了」，两边都得重扫一遍。"""
        体 = _函数体("PK")
        assert "甲名" in 体 and "乙名" in 体
        assert "的登录态失效" in 体


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
