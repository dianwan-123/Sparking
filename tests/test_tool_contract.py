import ast
import pathlib
import unittest


class ToolContractTests(unittest.TestCase):
    """LLM tools must RETURN results so the agent loop can reason over them.

    The `yield event.plain_result(...)` pattern sends the value straight to
    the chat and gives the agent "no return value", breaking multi-step flows.
    """

    def test_llm_tools_never_yield(self):
        main = pathlib.Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main.read_text(encoding="utf-8"))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if any("llm_tool" in d for d in decorators):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Yield, ast.YieldFrom)):
                        violations.append(node.name)
        self.assertEqual([], violations)

    def test_qzone_publish_prefers_native_napcat_action(self):
        source = pathlib.Path(__file__).resolve().parents[1] / "src" / "qq_services.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn('send_qzone_msg', text)
        self.assertIn('delete_qzone_msg', text)
        self.assertIn('get_qzone_msg_list', text)


if __name__ == "__main__":
    unittest.main()
