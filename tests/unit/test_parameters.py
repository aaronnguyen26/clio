"""Unit tests for ParameterEngine (FEAT-MEM-06).

Tests validate:
- Dynamic built-in tokens (${CURRENT_DATE}, ${CURRENT_TIME}, ${CURRENT_DATETIME}, etc.)
- User parameter interpolation across types (str, int, float, bool)
- List parameter formatting (bullet points, comma, newline, empty)
- Safe handling of missing parameters (token preservation)
- Escaping via $${var} and \\${var}
- Prevention of recursive/secondary token interpolation
- Token extraction and parameter validation utilities
"""

import datetime
import unittest

from src.memory.parameters import ParameterEngine


class TestParameterEngine(unittest.TestCase):
    """Test suite for ParameterEngine."""

    def setUp(self) -> None:
        self.fixed_dt = datetime.datetime(2026, 9, 23, 14, 30, 45)

    def test_builtin_date_token(self):
        """TEST-PARAM-01: ${CURRENT_DATE} interpolates to human-readable full date."""
        template = "Today is ${CURRENT_DATE}."
        output = ParameterEngine.interpolate(template, now=self.fixed_dt)
        self.assertEqual(output, "Today is Wednesday, September 23, 2026.")

    def test_builtin_time_token(self):
        """TEST-PARAM-02: ${CURRENT_TIME} interpolates to HH:MM:SS."""
        template = "Time: ${CURRENT_TIME}"
        output = ParameterEngine.interpolate(template, now=self.fixed_dt)
        self.assertEqual(output, "Time: 14:30:45")

    def test_builtin_datetime_token(self):
        """TEST-PARAM-03: ${CURRENT_DATETIME} interpolates to full date and time."""
        template = "Timestamp: ${CURRENT_DATETIME}"
        output = ParameterEngine.interpolate(template, now=self.fixed_dt)
        self.assertEqual(output, "Timestamp: Wednesday, September 23, 2026 14:30:45")

    def test_user_parameter_string(self):
        """TEST-PARAM-04: User string parameters interpolate cleanly."""
        template = "Hello, ${user_name}! Welcome to ${app_name}."
        params = {"user_name": "Alice", "app_name": "Notes"}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Hello, Alice! Welcome to Notes.")

    def test_user_parameter_scalar_types(self):
        """TEST-PARAM-05: Non-string scalars (int, float, bool) format to strings."""
        template = "Count: ${count}, Price: ${price}, Active: ${is_active}"
        params = {"count": 42, "price": 19.99, "is_active": True}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Count: 42, Price: 19.99, Active: True")

    def test_list_parameter_bullet_formatting(self):
        """TEST-PARAM-06: Lists format as bullet points by default."""
        template = "Tasks:\n${tasks}"
        params = {"tasks": ["Buy milk", "Review PR", "Send report"]}
        output = ParameterEngine.interpolate(template, params)
        expected = "Tasks:\n- Buy milk\n- Review PR\n- Send report"
        self.assertEqual(output, expected)

    def test_list_parameter_custom_format(self):
        """TEST-PARAM-07: Lists format as comma-separated or newline when configured."""
        template = "Items: ${items}"
        params = {"items": ["apple", "banana", "orange"]}
        comma_output = ParameterEngine.interpolate(template, params, list_format="comma")
        self.assertEqual(comma_output, "Items: apple, banana, orange")

        newline_output = ParameterEngine.interpolate(template, params, list_format="newline")
        self.assertEqual(newline_output, "Items: apple\nbanana\norange")

    def test_empty_list_parameter(self):
        """TEST-PARAM-08: Empty list interpolates to empty string."""
        template = "Items: [${items}]"
        params = {"items": []}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Items: []")

    def test_missing_parameter_preservation(self):
        """TEST-PARAM-09: Unsupplied parameters retain token representation verbatim."""
        template = "Note for ${author}: ${task_content} (priority: ${priority})"
        params = {"author": "Bob"}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Note for Bob: ${task_content} (priority: ${priority})")

    def test_dollar_escape_token(self):
        """TEST-PARAM-10: $${var} escapes to literal ${var} without interpolation."""
        template = "Literal $${doc_title} should not replace ${doc_title}."
        params = {"doc_title": "Project Plan"}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Literal ${doc_title} should not replace Project Plan.")

    def test_backslash_escape_token(self):
        """TEST-PARAM-11: \\${var} escapes to literal ${var} without interpolation."""
        template = "Escaped \\${CURRENT_DATE} and live ${CURRENT_DATE}."
        output = ParameterEngine.interpolate(template, now=self.fixed_dt)
        self.assertEqual(output, "Escaped ${CURRENT_DATE} and live Wednesday, September 23, 2026.")

    def test_prevention_of_recursive_substitution(self):
        """TEST-PARAM-12: Parameter value containing token syntax is not recursively resolved."""
        template = "Value: ${first}"
        params = {"first": "${second}", "second": "SHOULD_NOT_APPEAR"}
        output = ParameterEngine.interpolate(template, params)
        self.assertEqual(output, "Value: ${second}")

    def test_extract_tokens_utility(self):
        """TEST-PARAM-13: Extract tokens detects variables excluding escaped and builtins."""
        template = "Meeting ${title} on ${CURRENT_DATE} with ${attendees} ($${not_a_var})."
        tokens = ParameterEngine.extract_tokens(template)
        self.assertEqual(tokens, {"title", "attendees"})

    def test_validate_params_utility(self):
        """TEST-PARAM-14: Validate params identifies missing variables."""
        template = "Need ${a} and ${b} and ${c}."
        valid, missing = ParameterEngine.validate_params(template, {"a": "1", "c": "3"})
        self.assertFalse(valid)
        self.assertEqual(missing, ["b"])

        valid, missing = ParameterEngine.validate_params(template, {"a": "1", "b": "2", "c": "3"})
        self.assertTrue(valid)
        self.assertEqual(missing, [])
