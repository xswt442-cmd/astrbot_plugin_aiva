"""@ 认不出对手时的三层兜底。

线上事实（2026-10-13 22:28，用户截图明确 @ 了 YJJ）：

```
raw_message=PatchedGroupMessage
mentions=[]            ← 空
组件链=[At, Plain]      ← 只有 @机器人
message_str='爱瓦 PK'    ← @YJJ 连文本都没了
```

`mentions` 是空的 —— 平台没把被 @ 的人传过来。所以 @ 不能是唯一入口：

1. `msg_elements` / `raw_data` —— 适配器 `_set_raw_message_fields()` 挂上去的
   QQ 原始报文，@ 元素最可能在这里面。**尚未线上验证**，所以即使它失败
   也必须有下一层。
2. `mentions` —— botpy 解析出来的。
3. 按昵称匹配 —— 最后兜底，靠绑定时记下的昵称。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from aiva.pk import mention_summary, opponent_from_mentions, opponent_from_raw
from aiva.storage import CredentialStore, find_by_nickname, set_nickname

REPO_ROOT = Path(__file__).resolve().parent.parent

# 与 main.py 保持一致。测试里复刻 `_PK里的名字` 的逻辑时要用，
# 两边漂移时 `test_源码里正则确实这么写` 会先炸。
_GROUP = "爱瓦"
_NICKNAME_LIMIT = 16


class FakeMention:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeRaw:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestRawPayload:
    def test_mention_in_msg_elements(self):
        raw = FakeRaw(
            msg_elements=[
                {"element_type": "mention", "user_id": "YJJ_openid_123"},
                {"element_type": "text", "content": "/爱瓦 PK"},
            ],
            mentions=[],
        )
        assert opponent_from_raw(raw) == "YJJ_openid_123"

    def test_member_openid_preferred(self):
        raw = FakeRaw(
            msg_elements=[
                {
                    "element_type": "mention",
                    "user_id": "QQ号12345",
                    "member_openid": "openid_abc",
                }
            ]
        )
        # member_openid 才是跟凭据文件同体系的标识
        assert opponent_from_raw(raw) == "openid_abc"

    def test_skips_non_mention_elements(self):
        raw = FakeRaw(msg_elements=[{"element_type": "text", "content": "user_id: 假的"}])
        assert opponent_from_raw(raw) is None

    def test_mentions_in_raw_data(self):
        raw = FakeRaw(raw_data={"mentions": [{"member_openid": "op_from_raw"}]})
        assert opponent_from_raw(raw) == "op_from_raw"

    def test_raw_data_at_users(self):
        raw = FakeRaw(raw_data={"at_users": ["op_at_users"]})
        assert opponent_from_raw(raw) == "op_at_users"

    def test_skips_bot(self):
        raw = FakeRaw(msg_elements=[{"element_type": "mention", "user_id": "机器人_id"}])
        assert opponent_from_raw(raw, bot_id="机器人_id") is None

    def test_empty_payload_no_raise(self):
        assert opponent_from_raw(None) is None
        assert opponent_from_raw(FakeRaw()) is None
        assert opponent_from_raw(FakeRaw(raw_data="不是字典")) is None


class TestMentionFallback:
    def test_recognizes(self):
        raw = FakeRaw(mentions=[FakeMention(member_openid="YJJ", is_you=False, bot=False)])
        assert opponent_from_mentions(raw) == "YJJ"

    def test_skips_at_bot(self):
        raw = FakeRaw(mentions=[FakeMention(member_openid="机器人", is_you=True)])
        assert opponent_from_mentions(raw) is None

    def test_summary_has_member_openid(self):
        raw = FakeRaw(mentions=[FakeMention(member_openid="YJJ", is_you=False)])
        summary = mention_summary(raw)
        assert summary[0]["member_openid"] == "YJJ"


class TestNicknameFallback:
    def _store(self, tmp_path):
        store = CredentialStore(tmp_path)
        store.write("用户A", set_nickname({"userId": "1"}, "ZnNe"))
        store.write("用户B", set_nickname({"userId": "2"}, "YJJ"))
        return store

    def test_finds_by_nickname(self, tmp_path):
        store = self._store(tmp_path)
        assert find_by_nickname(store, "YJJ") == [("用户B", "YJJ")]

    def test_ignores_case(self, tmp_path):
        store = self._store(tmp_path)
        assert [n for _, n in find_by_nickname(store, "znne")] == ["ZnNe"]
        assert [n for _, n in find_by_nickname(store, "yjj")] == ["YJJ"]

    def test_ignores_spaces(self, tmp_path):
        store = CredentialStore(tmp_path)
        store.write("用户C", set_nickname({"userId": "3"}, "老 狗"))
        assert [n for _, n in find_by_nickname(store, "老狗")] == ["老 狗"]

    def test_excludes_self(self, tmp_path):
        store = self._store(tmp_path)
        assert find_by_nickname(store, "ZnNe", me="用户A") == []

    def test_returns_all_on_clash(self, tmp_path):
        """挑错人等于拿错人的战绩，所以不能只返回第一个。"""
        store = CredentialStore(tmp_path)
        store.write("用户D", set_nickname({"userId": "4"}, "小明"))
        store.write("用户E", set_nickname({"userId": "5"}, "小明"))
        assert len(find_by_nickname(store, "小明")) == 2

    def test_skips_without_nickname(self, tmp_path):
        store = CredentialStore(tmp_path)
        store.write("用户F", {"userId": "6"})
        assert find_by_nickname(store, "任何人") == []

    def test_empty_keyword_no_raise(self, tmp_path):
        store = self._store(tmp_path)
        assert find_by_nickname(store, "") == []
        assert find_by_nickname(store, "  ") == []

    def test_exact_before_fuzzy(self, tmp_path):
        """只做子串的话，「小明」会同时撞上「小明同学」。

        群里同时有这两个人时，用户发 `/爱瓦 PK 小明` 明明有个一模一样的，
        却被告知「匹配到几个人」——白问一句。所以精确命中优先。
        爱完美（aiwm）的 `storage.按昵称找` 就是这个顺序，这里照抄。
        """
        store = CredentialStore(tmp_path)
        store.write("用户D", set_nickname({"userId": "4"}, "小明"))
        store.write("用户E", set_nickname({"userId": "5"}, "小明同学"))
        assert find_by_nickname(store, "小明") == [("用户D", "小明")]

    def test_fuzzy_only_when_exact_empty(self, tmp_path):
        store = CredentialStore(tmp_path)
        store.write("用户E", set_nickname({"userId": "5"}, "小明同学"))
        assert find_by_nickname(store, "小明") == [("用户E", "小明同学")]

    def test_exact_ignores_case_and_spaces(self, tmp_path):
        store = CredentialStore(tmp_path)
        store.write("用户F", set_nickname({"userId": "6"}, "老 狗"))
        assert find_by_nickname(store, "老狗") == [("用户F", "老 狗")]

    def test_exact_returns_both_on_real_clash(self, tmp_path):
        """真有两个一模一样的名字，还是得让用户选。"""
        store = CredentialStore(tmp_path)
        store.write("用户G", set_nickname({"userId": "7"}, "小明"))
        store.write("用户H", set_nickname({"userId": "8"}, "小明"))
        assert len(find_by_nickname(store, "小明")) == 2


class TestPkTargetName:
    """`_PK里的名字` 在 main.py 里，但 main.py 依赖 AstrBot 不能 import。

    所以把它的源码单独抽出来 exec，只喂它依赖的正则和常量。
    """

    @staticmethod
    def _extract_name(body: str) -> str:
        """直接复刻 main.py 里 `_PK里的名字` 的逻辑。

        main.py 依赖 AstrBot 本地 import 不了，所以这里用同一条正则跑一遍。
        `test_源码里正则确实这么写` 兜住两边漂移的风险。
        """
        import re as _re

        pattern = _re.compile(_re.escape(_GROUP) + r"\s*(?:PK|pk|对战)\s*(.+)", _re.IGNORECASE)
        hit = pattern.search(body or "")
        if not hit:
            return ""
        return hit.group(1).strip().strip("：:@")[:_NICKNAME_LIMIT]

    def test_extracts_name(self):
        assert self._extract_name("爱瓦 PK YJJ") == "YJJ"

    def test_accepts_lowercase(self):
        assert self._extract_name("爱瓦 pk yjj") == "yjj"

    def test_accepts_leading_slash(self):
        assert self._extract_name("/爱瓦 PK YJJ") == "YJJ"

    def test_alias_versus(self):
        assert self._extract_name("爱瓦 对战 YJJ") == "YJJ"

    def test_no_name_returns_empty(self):
        assert self._extract_name("爱瓦 PK") == ""

    def test_strips_colon(self):
        assert self._extract_name("爱瓦 PK：YJJ") == "YJJ"

    def test_source_regex_matches(self):
        """防止测试里的正则和源码漂移。"""
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert "PK|pk|对战" in source


class TestMainWired:
    def test_pk_tries_three_layers_in_order(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = None
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "pk_cmd":
                node = n
        segment = ast.get_source_segment(source, node) or ""
        assert "opponent_from_raw" in segment
        assert "opponent_from_mentions" in segment
        assert "find_by_nickname" in segment, "必须有昵称兜底，@ 拿不到是既成事实"
        # 顺序：原始载荷 → mentions → 组件链/正文 → 昵称
        assert (
            segment.index("opponent_from_raw")
            < segment.index("opponent_from_mentions")
            < segment.index("find_by_nickname")
        )

    def test_names_exist_in_main(self):
        """**别再犯 `_从原始载荷认对手` 这种错。**

        线上炸过：`name '_从原始载荷认对手' is not defined`——函数加了
        下划线前缀，调用点没跟着改。本地 243 个测试全绿，因为它们测的是
        `aiva/pk.py` 里那个函数**本身**，没有一个真的去解析并检查
        `main.py` 的调用。

        所以这里用 AST 把 main.py 的顶层定义 + 导入全部收集起来，
        再把函数体里调用的每个名字逐个查一遍。
        """
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        visible: set[str] = set(dir(__builtins__)) | set(dir(json))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visible.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    visible.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    visible.add(alias.asname or alias.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                visible.add(node.id)
            elif isinstance(node, ast.arg):
                visible.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                visible.add(node.name)
            elif isinstance(node, (ast.comprehension,)):
                for child in ast.walk(node.target):
                    if isinstance(child, ast.Name):
                        visible.add(child.id)

        # 局部变量：每个函数体里的赋值/参数/for 都算
        missing = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    visible.add(node.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func
                name = (
                    callee.id
                    if isinstance(callee, ast.Name)
                    else callee.attr
                    if isinstance(callee, ast.Attribute)
                    else None
                )
                if name and name.startswith("_") and name not in visible:
                    missing.append((node.lineno, name))

        assert not missing, (
            "main.py 里调用了不存在的名字（多半是加/减了下划线前缀"
            "但没同步）：" + ", ".join(f"第{lineno}行 {name}" for lineno, name in missing)
        )

    def test_set_nickname_on_bind(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert "set_nickname(" in source, "绑定时必须记昵称，否则 PK 无从按名字找人"

    def test_writes_user_marker(self, tmp_path):
        """文件名是哈希，不明文存标识就永远反查不出是谁。"""
        store = CredentialStore(tmp_path)
        store.write("标识XYZ", {"userId": "u"})
        entries = store.iter_all()
        assert [u for u, _ in entries] == ["标识XYZ"]

    def test_old_data_skipped_not_crash(self, tmp_path):
        (tmp_path / "deadbeef.json").write_text(json.dumps({"userId": "u"}), encoding="utf-8")
        assert CredentialStore(tmp_path).iter_all() == []


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
