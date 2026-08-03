"""MCP 등록 계층 (§2.3).

description 이 비면 LLM 은 커서 규약도 stale 의 의미도 전혀 읽지 못한 채
툴을 호출한다. 그런데 그 실패는 조용하다 — 서버는 정상 기동하고 툴도 보인다.
docstring 뒤에 `% 변수` 같은 연산을 붙이면 __doc__ 이 None 이 되어 실제로
벌어지는 일이라, §2.3 이 이 검증을 테스트에 두라고 명시한다.

setup.sh 도 같은 검사를 하지만 그건 설치할 때만 돈다.
"""

from __future__ import annotations

import asyncio
import unittest

try:
    from revshell_mcp.manager import Config
    from revshell_mcp.server import build

    HAVE_MCP = True
except ModuleNotFoundError:  # pragma: no cover - mcp SDK 미설치 환경
    HAVE_MCP = False

EXPECTED = {
    "listener_start",
    "listener_stop",
    "session_list",
    "session_read",
    "session_write",
    "session_close",
    "session_exec",
}


@unittest.skipUnless(HAVE_MCP, "mcp SDK 가 없다")
class ServerRegistrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = {t.name: t for t in asyncio.run(build(Config()).list_tools())}

    def test_all_seven_tools_are_registered(self):
        self.assertEqual(set(self.tools), EXPECTED)

    def test_no_description_is_empty(self):
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertTrue(
                    (tool.description or "").strip(), f"{name} 의 description 이 비었다"
                )

    def test_cursor_contract_is_stated(self):
        # end_cursor 를 since 에 넣으면 그 사이 데이터가 통째로 유실된다 (§2.1)
        d = self.tools["session_read"].description
        self.assertIn("next_since", d)
        self.assertIn("null", d)
        self.assertIn("end_cursor", self.tools["session_list"].description)

    def test_untrusted_output_is_flagged(self):
        # §7 — 원격 출력이 LLM 컨텍스트로 직행한다
        for name in ("session_read", "session_exec"):
            with self.subTest(tool=name):
                self.assertIn("untrusted", self.tools[name].description)

    def test_liveness_fields_are_documented(self):
        """운용에서 걸린 지점 — active 를 믿었다가 죽은 세션을 계속 폴링했다."""
        d = self.tools["session_list"].description
        for token in ("stale", "last_rx_sec", "half-open", "age_sec"):
            with self.subTest(token=token):
                self.assertIn(token, d)

    def test_exec_hint_contract_mentions_stale(self):
        d = self.tools["session_exec"].description
        self.assertIn("stale", d)
        self.assertIn("background", d)

    def test_rearm_is_documented_on_both_sides(self):
        self.assertIn("rearm", self.tools["listener_start"].description)
        # 끄는 방법이 listener_stop 뿐이므로 그쪽에도 적혀 있어야 한다 (§4.11)
        self.assertIn("re-arming", self.tools["listener_stop"].description)

    def test_new_parameters_are_in_the_schema(self):
        # mcp 2.x 는 snake_case 다 (1.x 의 inputSchema 에서 바뀌었다)
        schema = self.tools["listener_start"].input_schema["properties"]
        self.assertIn("accept", schema)
        self.assertIn("rearm", schema)


if __name__ == "__main__":
    unittest.main()
