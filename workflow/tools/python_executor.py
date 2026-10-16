"""Python 代码执行工具：在子进程中隔离执行，带超时保护（复用自项目1）"""

import subprocess
import sys
import tempfile
import os
import logging
import re

logger = logging.getLogger(__name__)


class PythonExecutor:
    """在临时目录中用子进程执行 Python 代码，捕获 stdout/stderr。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    @staticmethod
    def validate_syntax(code: str) -> str:
        """Validate generated source without running interactive application code."""
        if not code or not code.strip():
            return "语法检查失败：代码为空。"
        try:
            compile(code, "<generated-solution>", "exec")
        except SyntaxError as exc:
            return (
                f"语法检查失败：第 {exc.lineno} 行第 {exc.offset} 列："
                f"{exc.msg}"
            )
        return "执行成功：Python 语法检查通过。"

    def run(self, code: str) -> str:
        if not code or not code.strip():
            return "错误：代码为空，未执行任何内容。"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout,
                cwd=os.path.dirname(tmp_path),
            )

            output_parts = []
            if result.stdout.strip():
                output_parts.append(f"[标准输出]\n{result.stdout.rstrip()}")
            if result.stderr.strip():
                output_parts.append(f"[错误输出]\n{result.stderr.rstrip()}")

            status = (
                "执行成功"
                if result.returncode == 0
                else f"执行失败（退出码 {result.returncode}）"
            )
            if output_parts:
                return f"{status}\n\n" + "\n\n".join(output_parts)
            return f"{status}（无输出）"

        except subprocess.TimeoutExpired:
            return (
                f"执行超时：代码运行超过 {self.timeout} 秒被强制终止。"
                "请检查是否有死循环或耗时操作。"
            )
        except Exception as exc:
            return f"执行环境异常：{type(exc).__name__}: {exc}"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def run_tests(
        self,
        solution_code: str,
        test_code: str,
        min_tests: int = 1,
        solution_filename: str = "solution.py",
    ) -> str:
        """Run generated unittest acceptance tests beside the candidate solution."""
        if min_tests < 1:
            raise ValueError("min_tests 必须大于 0")
        if not solution_code or not solution_code.strip():
            return "验收测试失败：候选代码为空，未运行测试。"
        if not test_code or not test_code.strip():
            return "验收测试失败：测试代码为空，未运行测试。"
        module_name, extension = os.path.splitext(solution_filename)
        if (
            extension != ".py"
            or not module_name.isidentifier()
            or os.path.basename(solution_filename) != solution_filename
        ):
            return "验收测试失败：候选实现文件名必须是安全的 Python 模块文件名。"

        try:
            with tempfile.TemporaryDirectory() as directory:
                solution_path = os.path.join(directory, solution_filename)
                test_path = os.path.join(directory, "test_solution.py")
                with open(solution_path, "w", encoding="utf-8") as solution_file:
                    solution_file.write(solution_code)
                with open(test_path, "w", encoding="utf-8") as test_file:
                    test_file.write(test_code)

                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "-v", "test_solution"],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    timeout=self.timeout,
                    cwd=directory,
                )

            output_parts = [
                output.rstrip()
                for output in (result.stdout, result.stderr)
                if output.strip()
            ]
            output = "\n\n".join(output_parts)
            summary = re.search(r"Ran (\d+) tests?", output)
            test_count = int(summary.group(1)) if summary else 0

            has_test_summary = summary is not None
            if result.returncode == 0 and test_count >= min_tests:
                status = f"验收测试通过（{test_count} 个用例）"
            elif has_test_summary and test_count < min_tests:
                status = (
                    f"验收测试失败：仅运行 {test_count} 个用例，"
                    f"至少需要 {min_tests} 个"
                )
            else:
                status = f"验收测试失败（退出码 {result.returncode}）"

            if output:
                return f"{status}\n\n{output}"
            return status
        except subprocess.TimeoutExpired:
            return (
                f"验收测试超时：运行超过 {self.timeout} 秒被强制终止。"
                "请检查测试或候选代码是否存在死循环或耗时操作。"
            )
        except Exception as exc:
            return f"验收测试环境异常：{type(exc).__name__}: {exc}"
