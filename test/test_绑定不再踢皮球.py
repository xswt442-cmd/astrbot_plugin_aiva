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

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = (REPO_ROOT / "main.py").read_text(encoding="utf-8")


def _body(name: str) -> str:
    tree = ast.parse(MAIN_SOURCE)
    node = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    assert node is not None, f"main.py 里找不到 {name}"
    segment = ast.get_source_segment(MAIN_SOURCE, node) or ""
    doc = ast.get_docstring(node, clean=False)
    return segment.replace(doc, "") if doc else segment


class TestBindStopsBlocking:
    def test_bind_checks_liveness_first(self):
        body = _body("bind")
        assert "_binding_alive(" in body, "绑定指令不能无条件挡已绑定的用户——他可能只是登录态过期了"

    def test_dead_binding_tells_user_to_rescan(self):
        body = _body("bind")
        assert "重新扫码就行" in body, "失效时要直接给出重扫这个动作"

    def test_blocks_only_when_alive(self):
        """换号才需要解绑。登录态过期不属于「换号」。"""
        body = _body("bind")
        assert "换号先发 /爱瓦 解绑" in body


class TestBindingAliveHitsApi:
    def test_does_not_only_check_main_role(self):
        """`get_main_role_raw` 过期了也回 result=0，拿它判断等于没判断。

        状态指令当初就是这么给出假绿灯的（见 test_状态同步与调模型.py）。
        """
        body = _body("_binding_alive")
        assert "_fetch_tracked(" in body, "必须打 record/list，那个才真的拒绝过期凭据"

    def test_login_expiry_and_api_error_both_mean_dead(self):
        body = _body("_binding_alive")
        assert "LoginExpired" in body
        assert "ApiError" in body

    def test_network_blip_counts_as_alive(self):
        """绑定指令只决定要不要挡人。抖动时宁可放行让人重扫，
        也别把人卡在「你得先解绑」。"""
        body = _body("_binding_alive")
        index = body.index("except Exception")
        assert "return True" in body[index:], "网络异常必须按「活着」处理，否则抖动会把人挡住"

    def test_incomplete_counts_as_dead(self):
        body = _body("_binding_alive")
        assert "is_complete(" in body


class TestExpiredTextGivesAction:
    def test_constant_carries_reason_and_action(self):
        """票据有固定有效期，只说「重新绑定」等于没说。"""
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")

        assert "重新发 /爱瓦 绑定" in source
        assert "三天" in source

    def test_matches_text(self):
        body = _body("_recent_stats")
        assert "_LOGIN_EXPIRED_HINT" in body, "要说清原因，别写死一句「登录态失效」"

    def test_status_text(self):
        body = _body("status_cmd")
        assert "_LOGIN_EXPIRED_HINT" in body

    def test_pk_names_who_expired(self):
        """以前只说「有人的登录态失效了」，两边都得重扫一遍。"""
        body = _body("pk_cmd")
        assert "my_name" in body and "rival_name" in body
        assert "的登录态失效" in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
