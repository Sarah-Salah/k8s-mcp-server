"""Audit logging for write operations.

The audit logger name ``k8s_mcp_server.audit`` is **part of the public
contract** — operators configure log aggregation against this name. Do not
rename it without a major version bump.
"""

from __future__ import annotations

import logging
import re
from typing import Any

__all__ = ["log_write_operation"]

logger = logging.getLogger("k8s_mcp_server.audit")

# Field-name-based redaction is intentional. Entropy-based detection causes
# false positives on UUIDs/hashes (which an LLM might want to log as a
# legitimate audit detail) and false negatives on field aliases (e.g.,
# "auth_header"). To extend protection, add the field-name pattern to
# _REDACT_PATTERN below. The pattern is documented in docs/SECURITY.md
# "Log redaction".
_REDACT_PATTERN = re.compile(r"(?i)\b(token|secret|password|api[_-]?key|bearer)(\s*[=:]\s*)\S+")


def log_write_operation(tool_name: str, **fields: Any) -> None:
    """Emit a structured INFO log line for a write tool attempt.

    Format: ``write_operation tool=<name> k1=v1 k2=v2 ...``. The
    ``write_operation`` prefix is stable so log aggregators can filter on
    it. Values are stringified; sensitive patterns are redacted via the
    SECURITY.md regex before emit.

    Called by every write tool as part of its audit contract — regardless
    of ``dry_run``. Fields are tool-specific (typically: ``namespace``,
    ``name``, ``dry_run``, plus tool-specific deltas like
    ``replicas_from``/``replicas_to``).
    """
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    line = f"write_operation tool={tool_name} {parts}".rstrip()
    logger.info(_REDACT_PATTERN.sub(r"\1\2<redacted>", line))
