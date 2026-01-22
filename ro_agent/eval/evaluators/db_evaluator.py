"""DBBench evaluation logic.

Ported from AgentBench's result_processor.py with adaptations for our use case.
"""

import ast
from typing import Any


class DBBenchEvaluator:
    """Evaluator for DBBench tasks.

    Compares agent answers to ground truth answers with appropriate
    handling for different query types and data formats.
    """

    @staticmethod
    def compare_results(
        answer: str | list[str] | None,
        ground_truth: list[str],
        query_type: str,
    ) -> bool:
        """Compare agent's answer to ground truth.

        Args:
            answer: The agent's submitted answer
            ground_truth: Expected answer(s) from the task
            query_type: Query type (SELECT, INSERT, UPDATE, DELETE)

        Returns:
            True if the answer matches ground truth
        """
        try:
            # Process both answers
            processed_answer = DBBenchEvaluator._clean_answer(answer)
            processed_ground_truth = DBBenchEvaluator._clean_answer(ground_truth)

            # For mutation queries, compare exact match (e.g., hash values)
            if query_type in ("INSERT", "DELETE", "UPDATE"):
                return processed_answer == processed_ground_truth

            # For SELECT queries, use more flexible comparison
            if len(processed_answer) == 1 and len(processed_ground_truth) == 1:
                ans_val = processed_answer[0]
                gt_val = processed_ground_truth[0]

                # Special value comparison
                if ans_val == "0" and gt_val == "0":
                    return True

                # Float comparison with tolerance
                if DBBenchEvaluator._is_float(ans_val) and DBBenchEvaluator._is_float(
                    gt_val
                ):
                    return DBBenchEvaluator._float_equal(ans_val, gt_val)

                # String comparison
                return ans_val == gt_val
            else:
                # Multiple values - check if all floats
                if all(
                    DBBenchEvaluator._is_float(x) for x in processed_answer
                ) and all(DBBenchEvaluator._is_float(x) for x in processed_ground_truth):
                    if len(processed_answer) != len(processed_ground_truth):
                        return False

                    # Match each answer to a ground truth value
                    matched_gt = [False] * len(processed_ground_truth)
                    for ans in processed_answer:
                        matched = False
                        for i, gt in enumerate(processed_ground_truth):
                            if not matched_gt[i] and DBBenchEvaluator._float_equal(
                                ans, gt
                            ):
                                matched_gt[i] = True
                                matched = True
                                break
                        if not matched:
                            return False
                    return all(matched_gt)

                # Set comparison for non-float values
                return set(processed_answer) == set(processed_ground_truth)

        except Exception:
            return False

    @staticmethod
    def _clean_answer(answer: Any) -> list[str]:
        """Clean and normalize an answer to a list of strings."""
        if answer is None:
            return ["0"]

        # Handle MySQL result format [(value,)]
        mysql_result = DBBenchEvaluator._clean_mysql_result(answer)
        if mysql_result is not None:
            return [DBBenchEvaluator._normalize_value(x) for x in mysql_result]

        if isinstance(answer, str):
            answer = answer.strip()

            # Handle string form of list
            if answer.startswith("[") and answer.endswith("]"):
                try:
                    # Try eval for Python literal
                    parsed = ast.literal_eval(answer)
                    if isinstance(parsed, list):
                        result = []
                        for item in parsed:
                            if isinstance(item, tuple) and len(item) == 1:
                                value = str(item[0]).strip().strip("'\"")
                                result.append(
                                    DBBenchEvaluator._normalize_value(value)
                                )
                            else:
                                value = str(item).strip().strip("'\"")
                                result.append(
                                    DBBenchEvaluator._normalize_value(value)
                                )
                        return result
                except Exception:
                    # Manual parsing
                    answer = answer[1:-1]
                    items = []
                    current = ""
                    in_quotes = False
                    for char in answer:
                        if char in "\"'":
                            in_quotes = not in_quotes
                        elif char == "," and not in_quotes:
                            if current:
                                items.append(
                                    DBBenchEvaluator._normalize_value(
                                        current.strip().strip("'\"")
                                    )
                                )
                                current = ""
                        else:
                            current += char
                    if current:
                        items.append(
                            DBBenchEvaluator._normalize_value(
                                current.strip().strip("'\"")
                            )
                        )
                    return items
            else:
                # Single value
                return [
                    DBBenchEvaluator._normalize_value(answer.strip().strip("'\""))
                ]

        elif isinstance(answer, (list, tuple)):
            result = []
            for item in answer:
                if isinstance(item, tuple) and len(item) == 1:
                    value = str(item[0]).strip().strip("'\"")
                    result.append(DBBenchEvaluator._normalize_value(value))
                else:
                    value = str(item).strip().strip("'\"")
                    result.append(DBBenchEvaluator._normalize_value(value))
            return result
        else:
            return [
                DBBenchEvaluator._normalize_value(str(answer).strip().strip("'\""))
            ]

    @staticmethod
    def _clean_mysql_result(result: Any) -> list[str] | None:
        """Handle MySQL result format [(value,)]."""
        if (
            isinstance(result, str)
            and result.startswith("[")
            and result.endswith("]")
        ):
            try:
                parsed = ast.literal_eval(result)
                if isinstance(parsed, list) and all(
                    isinstance(item, tuple) for item in parsed
                ):
                    values = []
                    for item in parsed:
                        if len(item) == 1:
                            value = str(item[0]).strip().strip("'\"")
                            values.append(value)
                    return values
            except Exception:
                pass

            # Try single tuple format
            try:
                stripped = result.strip("[]")
                if (
                    stripped.count("(") == 1
                    and stripped.startswith("(")
                    and stripped.endswith(",)")
                ):
                    value = stripped[1:-2].strip().strip("'\"")
                    return [value]
            except Exception:
                pass

        return None

    @staticmethod
    def _normalize_value(value: Any) -> str:
        """Normalize special values, percentages, and formatted numbers."""
        if value is None:
            return "0"

        str_value = str(value).strip()

        # Handle percentage
        if str_value.endswith("%"):
            str_value = str_value[:-1].strip()

        # Handle thousand separators
        if (
            "," in str_value
            and not str_value.startswith("[")
            and not str_value.endswith("]")
        ):
            str_value = str_value.replace(",", "")

        # Map special values
        lower_value = str_value.lower()
        special_map = {
            "none": "0",
            "null": "0",
            "undefined": "0",
            "nan": "0",
            "inf": "0",
            "infinity": "0",
            "-inf": "0",
            "-infinity": "0",
            "": "0",
        }

        return special_map.get(lower_value, str_value)

    @staticmethod
    def _is_float(value: str) -> bool:
        """Check if value can be converted to float."""
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _float_equal(a: str, b: str, tolerance: float = 0.01) -> bool:
        """Compare two float values with tolerance."""
        try:
            return abs(float(a) - float(b)) <= tolerance
        except (ValueError, TypeError):
            return False

    @staticmethod
    def compare_hash(calculated: str | None, expected: str | None) -> bool:
        """Compare calculated hash to expected answer_md5.

        The expected format from the dataset is MySQL result format:
        "[('fa81a61f9a648475594128fa51bfa80d',)]"

        Args:
            calculated: Hash calculated from table state
            expected: answer_md5 from dataset

        Returns:
            True if hashes match
        """
        if calculated is None or expected is None:
            return False

        # Extract hash from MySQL result format
        cleaned = expected.strip()

        # Handle "[('hash',)]" format
        if cleaned.startswith("[") and cleaned.endswith(")]"):
            try:
                parsed = ast.literal_eval(cleaned)
                if isinstance(parsed, list) and len(parsed) == 1:
                    if isinstance(parsed[0], tuple) and len(parsed[0]) == 1:
                        cleaned = str(parsed[0][0])
            except Exception:
                # Manual extraction
                cleaned = cleaned.strip("[]() '\",")

        # Also handle simpler formats
        cleaned = cleaned.strip("[]() '\",")

        return calculated.lower() == cleaned.lower()
