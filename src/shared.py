"""Shared globals, imports, and debug logging for the Alcatraz Setup Wizard."""

import os
import sys
import re
import subprocess
import shutil
import json
import platform
import shlex
import textwrap
import time
import threading
import queue
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.rule import Rule
from rich.prompt import Prompt, Confirm
from rich.style import Style
from rich.align import Align
from rich.padding import Padding
from rich import box
from rich.live import Live
import questionary
from questionary import Style as QStyle
from prompt_toolkit.application import Application as PTApp
from prompt_toolkit.key_binding import KeyBindings as PTKeyBindings
from prompt_toolkit.layout import Layout as PTLayout, Window as PTWindow
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style as PTStyle

# ── Globals ───────────────────────────────────────────────────────
console = Console()
VERSION = "1.1.0"
TOTAL_STEPS = 13
RECOMMENDED_TOTAL_STEPS = 4

# ── Questionary custom style ─────────────────────────────────────
q_style = QStyle([
    ("qmark", "fg:#61afef bold"),
    ("question", "fg:#e5c07b bold"),
    ("answer", "fg:#98c379 bold"),
    ("pointer", "fg:#61afef bold"),
    ("highlighted", "fg:#61afef bold"),
    ("selected", "fg:#98c379"),
    ("separator", "fg:#6c6c6c"),
    ("instruction", "fg:#6c6c6c"),
    ("text", "fg:#abb2bf"),
])

# ── Debug tracing ─────────────────────────────────────────────────
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wizard_debug.log")
_SENSITIVE_RE = re.compile(r'(token|key|password|secret|credential|pat)[=: ]+\S+', re.IGNORECASE)

def _dbg(msg: str):
    sanitized = _SENSITIVE_RE.sub(r'\1=***REDACTED***', msg)
    with open(_DEBUG_LOG, "a") as f:
        f.write(f"{sanitized}\n")
