"""Variable token interpolation engine for dynamic workflow templates.

Belongs to FEAT-MEM-06 (ParameterEngine).
Supports:
- Built-in dynamic tokens (${CURRENT_DATE}, ${CURRENT_TIME}, ${CURRENT_DATETIME}, etc.)
- User runtime parameters (${doc_title}, ${user_name}, etc.)
- List value formatting (bullet points, comma-separated, newlines)
- Safe handling of missing parameters (retaining ${unresolved_token})
- Token escaping ($${var} -> ${var}, \\${var} -> ${var})
- Single-pass replacement preventing recursive injection
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class ParameterEngine:
    """Variable token interpolation engine (${var}, ${CURRENT_DATE})."""

    # Dynamic token formatters: token_name -> callable(datetime) -> str
    BUILTIN_TOKEN_FORMATS: Dict[str, Callable[[datetime.datetime], str]] = {
        "CURRENT_DATE": lambda dt: dt.strftime("%A, %B %d, %Y"),
        "CURRENT_TIME": lambda dt: dt.strftime("%H:%M:%S"),
        "CURRENT_DATETIME": lambda dt: dt.strftime("%A, %B %d, %Y %H:%M:%S"),
        "CURRENT_ISO_DATE": lambda dt: dt.date().isoformat(),
        "CURRENT_ISO_DATETIME": lambda dt: dt.isoformat(),
        "CURRENT_YEAR": lambda dt: dt.strftime("%Y"),
        "CURRENT_MONTH": lambda dt: dt.strftime("%B"),
        "CURRENT_DAY": lambda dt: dt.strftime("%d"),
    }

    # Regex matching:
    # 1. Backslash escaped: \${var} -> group(1)='\\', group(2)='', group(3)='var'
    # 2. Dollar escaped: $${var} -> group(1)='', group(2)='$$', group(3)='var'
    # 3. Unescaped: ${var} -> group(1)='', group(2)='$', group(3)='var'
    TOKEN_PATTERN = re.compile(r"(\\)?(\${1,2})\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    @classmethod
    def interpolate(
        cls,
        template: str,
        runtime_params: Optional[Dict[str, Any]] = None,
        now: Optional[datetime.datetime] = None,
        list_format: str = "bullet",
    ) -> str:
        """Interpolates tokens in template using runtime_params and built-in dynamic tokens.

        Args:
            template: The string containing variable tokens (${var}, $${escaped}).
            runtime_params: Key-value mapping of parameters.
            now: Optional datetime for deterministic built-in token resolution.
            list_format: How list values are rendered:
                         'bullet' ("- item1\n- item2"),
                         'comma' ("item1, item2"),
                         'newline' ("item1\nitem2").

        Returns:
            The interpolated string with tokens substituted or safely preserved.
        """
        if not template:
            return ""

        if runtime_params is None:
            runtime_params = {}

        if now is None:
            now = datetime.datetime.now()

        # Compute dynamic built-in values
        dynamic_values: Dict[str, str] = {
            name: fmt(now) for name, fmt in cls.BUILTIN_TOKEN_FORMATS.items()
        }

        def replacer(match: re.Match) -> str:
            backslash = match.group(1)
            dollars = match.group(2)
            key = match.group(3)

            # Escaped with backslash: \${var} -> ${var}
            if backslash:
                return f"${{{key}}}"

            # Escaped with double dollar: $${var} -> ${var}
            if dollars == "$$":
                return f"${{{key}}}"

            # Check dynamic built-ins
            if key in dynamic_values:
                return dynamic_values[key]

            # Check user runtime_params
            if key in runtime_params:
                val = runtime_params[key]
                return cls._format_value(val, list_format=list_format)

            # Missing parameter: retain verbatim ${key}
            return match.group(0)

        # Single-pass regex substitution prevents recursive template injection
        return cls.TOKEN_PATTERN.sub(replacer, template)

    @classmethod
    def _format_value(cls, value: Any, list_format: str = "bullet") -> str:
        """Formats a parameter value into its string representation."""
        if value is None:
            return ""

        if isinstance(value, (list, tuple)):
            if not value:
                return ""
            if list_format == "comma":
                return ", ".join(str(item) for item in value)
            elif list_format == "newline":
                return "\n".join(str(item) for item in value)
            else:  # default 'bullet'
                return "\n".join(f"- {item}" for item in value)

        return str(value)

    @classmethod
    def extract_tokens(cls, template: str, include_builtins: bool = False) -> Set[str]:
        """Extracts all variable token names required by a template.

        Excludes escaped tokens ($${var} or \\${var}).
        """
        if not template:
            return set()

        tokens: Set[str] = set()
        for match in cls.TOKEN_PATTERN.finditer(template):
            backslash = match.group(1)
            dollars = match.group(2)
            key = match.group(3)

            if backslash or dollars == "$$":
                continue

            if not include_builtins and key in cls.BUILTIN_TOKEN_FORMATS:
                continue

            tokens.add(key)

        return tokens

    @classmethod
    def validate_params(
        cls, template: str, runtime_params: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, List[str]]:
        """Checks if all required user variables in template are provided in runtime_params.

        Returns:
            Tuple of (is_valid, list_of_missing_keys).
        """
        if runtime_params is None:
            runtime_params = {}

        required = cls.extract_tokens(template, include_builtins=False)
        missing = [key for key in sorted(required) if key not in runtime_params]
        return len(missing) == 0, missing
