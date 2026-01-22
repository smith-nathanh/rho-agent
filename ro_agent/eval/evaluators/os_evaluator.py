"""OS Interaction evaluation logic."""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tasks.os_interaction import EvaluationConfig


class OSEvaluator:
    """Evaluator for OS Interaction tasks.

    Supports two evaluation modes:
    - match: Direct string comparison
    - check: Run check scripts (integer-match, string-match, containing, etc.)
    """

    def __init__(self, scripts_dir: Path | str | None = None) -> None:
        """Initialize the OS evaluator.

        Args:
            scripts_dir: Directory containing check scripts
        """
        self._scripts_dir = Path(scripts_dir) if scripts_dir else None

    async def evaluate(
        self,
        answer: str | None,
        eval_config: "EvaluationConfig",
        container=None,  # Optional: EvalContainer for running scripts in container
        scripts_dir: Path | str | None = None,  # Override scripts directory for this evaluation
    ) -> bool:
        """Evaluate an agent's answer.

        Args:
            answer: The agent's submitted answer
            eval_config: Evaluation configuration from the task
            container: Optional container for running check scripts
            scripts_dir: Override scripts directory (uses instance default if not provided)

        Returns:
            True if the answer is correct
        """
        if answer is None:
            return False

        # Use provided scripts_dir or fall back to instance default
        effective_scripts_dir = Path(scripts_dir) if scripts_dir else self._scripts_dir

        if eval_config.eval_type == "match":
            return self._evaluate_match(answer, eval_config)
        elif eval_config.eval_type == "check":
            return await self._evaluate_check(answer, eval_config, container, effective_scripts_dir)
        else:
            return False

    def _evaluate_match(
        self, answer: str, eval_config: "EvaluationConfig"
    ) -> bool:
        """Evaluate using match criteria (exact or regex)."""
        import re

        # Apply strip if configured
        if eval_config.match_strip:
            answer = answer.strip()

        # Regex match
        if eval_config.match_regex:
            return re.search(eval_config.match_regex, answer) is not None

        # Exact match
        if eval_config.match_answer is not None:
            expected = eval_config.match_answer
            if eval_config.match_strip:
                expected = expected.strip()
            return answer == expected

        return False

    def _normalize_string(self, s: str) -> str:
        """Normalize a string for comparison."""
        # Normalize newlines
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        # Strip whitespace
        return s.strip()

    async def _evaluate_check(
        self,
        answer: str,
        eval_config: "EvaluationConfig",
        container=None,
        scripts_dir: Path | None = None,
    ) -> bool:
        """Evaluate using check scripts.

        Follows AgentBench's chaining logic:
        - params starts as [answer]
        - For each check script:
          - If script is None (empty file), run example_script instead
          - Execute script with *params as arguments
          - Append stdout to params for next script
        - All scripts must exit 0 to pass
        """
        if not eval_config.check_scripts:
            return False

        # Params accumulate: [answer, output1, output2, ...]
        params = [str(answer)]

        for check_script in eval_config.check_scripts:
            # If check_script.file is empty, use example_script (AgentBench's null behavior)
            if not check_script.file:
                if eval_config.example_script and container:
                    stdout = await self._run_example_script(
                        eval_config.example_script, container
                    )
                    if stdout is None:
                        return False
                    params.append(stdout)
                continue

            # Run the check script with accumulated params
            success, stdout = await self._run_check_script_chained(
                params,
                check_script.file,
                container,
                scripts_dir,
            )

            if not success:
                return False

            # Append output for next script
            params.append(stdout)

        return True

    async def _run_example_script(
        self, example_config: dict, container
    ) -> str | None:
        """Run an example script to get expected value."""
        if not example_config:
            return None

        try:
            # Handle inline code (most common in AgentBench)
            if "code" in example_config:
                code = example_config["code"]
                exit_code, stdout, stderr = await container.execute(
                    code,
                    timeout=30,
                )
                if exit_code == 0:
                    return stdout.strip()

            # Handle file-based scripts
            elif "file" in example_config:
                script_file = example_config["file"]
                exit_code, stdout, stderr = await container.execute(
                    f"python3 {script_file}",
                    timeout=30,
                )
                if exit_code == 0:
                    return stdout.strip()

        except Exception:
            pass

        return None

    async def _run_check_script_chained(
        self,
        params: list[str],
        script_path: str,
        container=None,
        scripts_dir: Path | None = None,
    ) -> tuple[bool, str]:
        """Run a single check script with chained params.

        Args:
            params: List of arguments [answer, output1, output2, ...]
            script_path: Path to check script
            container: Container to run in
            scripts_dir: Directory containing scripts

        Returns:
            Tuple of (success, stdout) for chaining
        """
        script_name = Path(script_path).name

        # Try to run in container if we have the scripts locally
        if scripts_dir:
            local_script = scripts_dir / script_path
            if local_script.exists():
                return await self._run_check_in_container_chained(
                    params, local_script, container
                )

        # Fallback to builtin checks (for common scripts)
        # These expect (answer, expected) format
        if len(params) >= 2:
            answer, expected = params[0], params[1]
        else:
            answer, expected = params[0], ""

        success = self._run_builtin_check(answer, expected, script_name)
        return success, ""

    async def _run_check_in_container_chained(
        self, params: list[str], script_path: Path, container
    ) -> tuple[bool, str]:
        """Run a check script inside the container with chained params.

        Copies the script to the container and executes it with all params as arguments.
        Returns (success, stdout) for chaining to next script.
        """
        import shlex
        import base64

        if not container:
            # Fallback to local execution
            return self._run_local_check_chained(params, script_path)

        script_ext = script_path.suffix.lower()
        script_content = script_path.read_text()

        # Escape all params for shell
        params_escaped = " ".join(shlex.quote(p) for p in params)

        # Encode script content to avoid escaping issues
        script_b64 = base64.b64encode(script_content.encode()).decode()

        if script_ext == ".sh":
            # Some shell scripts don't take args (like checking/0.sh that tests commands)
            if "$1" in script_content or "$2" in script_content:
                cmd = f"echo {script_b64} | base64 -d > /tmp/check.sh && chmod +x /tmp/check.sh && /tmp/check.sh {params_escaped}"
            else:
                cmd = f"echo {script_b64} | base64 -d > /tmp/check.sh && chmod +x /tmp/check.sh && /tmp/check.sh"
        elif script_ext == ".py":
            cmd = f"echo {script_b64} | base64 -d > /tmp/check.py && python3 /tmp/check.py {params_escaped}"
        else:
            return False, ""

        exit_code, stdout, stderr = await container.execute(cmd, timeout=60)
        return exit_code == 0, stdout.strip() if stdout else ""

    def _run_local_check_chained(
        self, params: list[str], script_path: Path
    ) -> tuple[bool, str]:
        """Fallback: run check script locally with params."""
        try:
            script_ext = script_path.suffix.lower()

            if script_ext == ".py":
                result = subprocess.run(
                    ["python3", str(script_path)] + params,
                    capture_output=True,
                    timeout=30,
                )
                return result.returncode == 0, result.stdout.decode().strip()

            if script_ext == ".sh":
                result = subprocess.run(
                    ["bash", str(script_path)] + params,
                    capture_output=True,
                    timeout=30,
                )
                return result.returncode == 0, result.stdout.decode().strip()

            return False, ""
        except Exception:
            return False, ""

    def _run_builtin_check(self, answer: str, expected: str, script_name: str) -> bool:
        """Run built-in check implementations."""
        # Normalize inputs
        answer = self._normalize_string(answer)
        expected = self._normalize_string(expected)

        if script_name == "integer-match.py":
            try:
                return int(answer) == int(expected)
            except ValueError:
                return False

        elif script_name == "string-match.py":
            return answer == expected

        elif script_name == "containing.py":
            # Check if expected is contained in answer
            return expected in answer

        elif script_name == "in.py":
            # Check if answer is contained in expected
            return answer in expected

        elif script_name == "size-match.py":
            return self._size_match(answer, expected)

        else:
            # Default to string match
            return answer == expected

    def _size_match(self, answer: str, expected: str) -> bool:
        """Compare file sizes with unit conversion."""
        units = {
            "B": 1,
            "Byte": 1,
            "K": 1024,
            "KB": 1024,
            "M": 1024 * 1024,
            "MB": 1024 * 1024,
            "G": 1024 * 1024 * 1024,
            "GB": 1024 * 1024 * 1024,
            "T": 1024**4,
            "TB": 1024**4,
            "P": 1024**5,
            "PB": 1024**5,
        }

        def parse_size(s: str) -> int:
            s = s.strip()
            for unit, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
                if s.endswith(unit):
                    try:
                        return int(s[: -len(unit)]) * multiplier
                    except ValueError:
                        return -1
            try:
                return int(s)
            except ValueError:
                return -1

        return parse_size(answer) == parse_size(expected)
