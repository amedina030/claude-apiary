#!/usr/bin/env python3
"""
PostToolUse hook — no-op.

Logging is now handled by pre_tool_use.py (PRE-to-PRE delta) and stop_session.py
(final entry). This file is kept registered to avoid breaking existing installations.
"""
import sys

sys.exit(0)
