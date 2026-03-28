#!/usr/bin/env python3
"""
Alcatraz Setup Wizard
================================
A WinRAR-style setup wizard for configuring Dockerized Claude Code
with Git Guardian, security layers, and team-safe defaults.

Requires: Python 3.8+, rich, questionary
Bootstrap via: ./install.sh
"""

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

# ── Dependency check ──────────────────────────────────────────────
try:
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
except ImportError:
    print("\n  Missing dependencies. Run: pip install rich questionary\n")
    sys.exit(1)

# ── Globals ───────────────────────────────────────────────────────
console = Console()
VERSION = "1.1.0"
TOTAL_STEPS = 15

# ── Debug tracing ─────────────────────────────────────────────────
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wizard_debug.log")
_SENSITIVE_RE = re.compile(r'(token|key|password|secret|credential|pat)[=: ]+\S+', re.IGNORECASE)

def _dbg(msg: str):
    sanitized = _SENSITIVE_RE.sub(r'\1=***REDACTED***', msg)
    with open(_DEBUG_LOG, "a") as f:
        f.write(f"{sanitized}\n")


# ── Input Validation ─────────────────────────────────────────────
_BRANCH_RE = re.compile(r'^[a-zA-Z0-9./_-]+$')
_MEMORY_RE = re.compile(r'^[0-9]+[bBkKmMgG]$')


def validate_branch_name(name: str) -> bool:
    """Reject branch names that could inject shell code into case statements."""
    return bool(_BRANCH_RE.match(name)) and len(name) <= 128


def validate_memory_limit(value: str) -> bool:
    """Validate Docker memory limit format (e.g., '8g', '512m') and cap at 64g."""
    if not _MEMORY_RE.match(value):
        return False
    num = int(re.match(r'^(\d+)', value).group(1))
    suffix = value[-1].lower()
    # Convert to GB for cap check
    gb = num if suffix == 'g' else num / 1024 if suffix == 'm' else 0
    return 0 < gb <= 64


def validate_port(port_str: str) -> bool:
    """Validate port is an integer in the unprivileged range 1024-65535."""
    if not port_str.isdigit():
        return False
    port = int(port_str)
    return 1024 <= port <= 65535

# Questionary custom style to match our theme
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


# ══════════════════════════════════════════════════════════════════
#  DATA CLASSES — Configuration State
# ══════════════════════════════════════════════════════════════════

@dataclass
class SetupConfig:
    """All user choices collected during the wizard."""
    # Paths
    install_dir: str = ""
    # Profile
    profile: str = "recommended"  # recommended | minimal | full | custom
    # Dockerfile sections
    include_cloud_clis: bool = True
    include_infra_tools: bool = True
    include_ml_packages: bool = False
    include_browser: bool = True
    include_docker_cli: bool = True
    include_github_cli: bool = True
    include_db_clients: bool = True
    # Git Guardian
    protected_branches: list = field(default_factory=lambda: ["main", "master", "develop", "production", "release"])
    guardian_confirm_all_pushes: bool = False
    guardian_auto_allow_claude_branches: bool = True
    # Network & Ports
    default_network: str = "bridge"
    port_mode: str = "deterministic"  # deterministic | fixed | noports
    ports: list = field(default_factory=lambda: [3000, 3001, 5173, 8080])
    custom_ports: list = field(default_factory=list)
    # Security
    enable_deny_list: bool = True
    enable_pretool_hook: bool = True
    enable_session_timeout: bool = False
    session_timeout_hours: int = 4
    enable_readonly_hooks: bool = False
    enable_resource_limits: bool = False
    resource_memory: str = "8g"
    resource_cpus: int = 4
    # PAT type
    pat_type: str = "fine-grained"  # fine-grained | classic
    # Claude auth
    auth_method: str = "oauth"  # oauth | api_key
    # OS detected
    os_type: str = ""
    is_wsl: bool = False
    # Step completion tracking
    completed_steps: set = field(default_factory=set)

    STEP_NAMES = {
        0: "Installation Directory",
        1: "Setup Profile",
        2: "Git Guardian",
        3: "Network & Ports",
        4: "Security & Auth",
        # Post-generation steps
        5: "GitHub PAT",
        6: "Token Storage",
        7: "Docker Build",
        8: "Claude Auth",
        9: "Project Settings",
        10: "Branch Protection",
        11: "First Launch",
        12: "Install Launcher",
        13: "Daily Workflow",
    }

    def mark_complete(self, step_index: int):
        self.completed_steps.add(step_index)

    def is_step_complete(self, step_index: int) -> bool:
        return step_index in self.completed_steps

    def incomplete_steps(self) -> list:
        """Only checks config steps 0-4 (needed before file generation)."""
        return [i for i in range(5) if i not in self.completed_steps]

    def all_complete(self) -> bool:
        return len(self.completed_steps) >= 5


# ══════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def show_banner():
    banner_text = """
 █████╗ ██╗      ██████╗ █████╗ ████████╗██████╗  █████╗ ███████╗
██╔══██╗██║     ██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗╚══███╔╝
███████║██║     ██║     ███████║   ██║   ██████╔╝███████║  ███╔╝
██╔══██║██║     ██║     ██╔══██║   ██║   ██╔══██╗██╔══██║ ███╔╝
██║  ██║███████╗╚██████╗██║  ██║   ██║   ██║  ██║██║  ██║███████╗
╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝
    """
    console.print(Panel(
        Align.center(Text(banner_text, style="bold cyan") +
                     Text("\nAlcatraz Setup Wizard", style="bold white") +
                     Text(f"  v{VERSION}\n", style="dim")),
        border_style="cyan",
        box=box.DOUBLE,
        padding=(0, 2),
    ))


def show_step_header(step_num: int, total: int, title: str, subtitle: str = ""):
    bar_filled = "█" * step_num
    bar_empty = "░" * (total - step_num)
    progress_bar = f"[cyan]{bar_filled}[/][dim]{bar_empty}[/]"
    pct = int((step_num / total) * 100)

    console.print()
    console.print(Rule(style="dim cyan"))
    console.print(
        f"  [bold cyan]Step {step_num}[/] of {total}  {progress_bar}  [dim]{pct}%[/]"
    )
    console.print(f"  [bold white]{title}[/]")
    if subtitle:
        console.print(f"  [dim]{subtitle}[/]")
    console.print(Rule(style="dim cyan"))
    console.print()


def show_info_box(title: str, content: str, style: str = "cyan"):
    console.print(Panel(
        content,
        title=f"[bold]{title}[/]",
        border_style=style,
        padding=(1, 2),
    ))


def show_check(label: str, passed: bool, detail: str = ""):
    icon = "[bold green]✓[/]" if passed else "[bold red]✗[/]"
    status = "[green]Found[/]" if passed else "[red]Missing[/]"
    detail_str = f" [dim]({detail})[/]" if detail else ""
    console.print(f"  {icon} {label:<30} {status}{detail_str}")


def pause():
    console.print()
    console.input("  [dim]Press Enter to continue[/]")


def step_menu(choices, initial_nav=0, continue_label="Continue"):
    """Unified step menu with horizontal navigation bar at the bottom.

    choices: list of (label, value) tuples for step options (e.g., edit actions).
    initial_nav: 0=Continue (default), 1=Back — which nav button to pre-select.

    Navigation:
      ↑ ↓   — move between step options and the nav bar
      ← →   — toggle between Back and Continue (when on the nav bar)
      Enter  — confirm selection

    Returns the selected option value, "back", or "next".
    """
    nav_row = len(choices)
    total_rows = nav_row + 1
    selected_row = [nav_row]          # Start on the nav bar
    nav_selected = [initial_nav]      # 0=Continue, 1=Back

    menu_style = PTStyle.from_dict({
        "pointer": "fg:#61afef bold",
        "highlighted": "fg:#61afef bold",
        "separator": "fg:#6c6c6c",
        "nav.active": "bold reverse",
        "nav.inactive": "fg:#6c6c6c",
        "nav.dim": "fg:#4b5263",
        "hint": "fg:#6c6c6c italic",
    })

    def get_text():
        lines = [("", "\n")]

        # Step options
        for i, (label, _value) in enumerate(choices):
            if i == selected_row[0] and selected_row[0] < nav_row:
                lines += [("class:pointer", "  ❯ "), ("class:highlighted", label)]
            else:
                lines += [("", "    "), ("", label)]
            lines.append(("", "\n"))

        # Separator
        lines.append(("class:separator", "    ─────────────────────────────\n"))

        # Horizontal nav bar
        on_nav = selected_row[0] == nav_row
        if on_nav:
            back_cls = "class:nav.active" if nav_selected[0] == 1 else "class:nav.inactive"
            cont_cls = "class:nav.active" if nav_selected[0] == 0 else "class:nav.inactive"
        else:
            back_cls = cont_cls = "class:nav.dim"

        lines += [
            ("class:pointer" if on_nav else "", "  ❯ " if on_nav else "    "),
            (back_cls, " ← Back "),
            ("", "     "),
            (cont_cls, f" → {continue_label} "),
            ("", "\n\n"),
        ]

        # Context hint
        if on_nav:
            lines.append(("class:hint", "    ← → switch   Enter confirm   ↑ options"))
        else:
            lines.append(("class:hint", "    ↑ ↓ navigate   Enter select   ↓ navigation"))
        lines.append(("", "\n"))

        return lines

    kb = PTKeyBindings()

    _dbg(f"[step_menu] choices={[c[1] for c in choices]}, nav_row={nav_row}, total_rows={total_rows}")

    @kb.add("up")
    def _up(event):
        old = selected_row[0]
        if selected_row[0] > 0:
            selected_row[0] -= 1
        _dbg(f"  [KEY] up: row {old} -> {selected_row[0]}")
        event.app.invalidate()

    @kb.add("down")
    def _down(event):
        old = selected_row[0]
        if selected_row[0] < total_rows - 1:
            selected_row[0] += 1
        _dbg(f"  [KEY] down: row {old} -> {selected_row[0]}")
        event.app.invalidate()

    @kb.add("left")
    def _left(event):
        old = nav_selected[0]
        if selected_row[0] == nav_row:
            nav_selected[0] = 1
        _dbg(f"  [KEY] left: on_nav={selected_row[0]==nav_row}, nav {old} -> {nav_selected[0]}")
        event.app.invalidate()

    @kb.add("right")
    def _right(event):
        old = nav_selected[0]
        if selected_row[0] == nav_row:
            nav_selected[0] = 0
        _dbg(f"  [KEY] right: on_nav={selected_row[0]==nav_row}, nav {old} -> {nav_selected[0]}")
        event.app.invalidate()

    @kb.add("enter")
    def _enter(event):
        if selected_row[0] < nav_row:
            val = choices[selected_row[0]][1]
            _dbg(f"  [KEY] enter: row={selected_row[0]} < nav_row={nav_row}, returning choice {val!r}")
            event.app.exit(result=val)
        else:
            val = "back" if nav_selected[0] == 1 else "next"
            _dbg(f"  [KEY] enter: row={selected_row[0]} == nav_row, nav_selected={nav_selected[0]}, returning {val!r}")
            event.app.exit(result=val)

    @kb.add("c-c")
    def _cancel(event):
        raise KeyboardInterrupt()

    height = len(choices) + 6  # options + separator + nav + hint + padding
    app = PTApp(
        layout=PTLayout(PTWindow(content=FormattedTextControl(get_text, show_cursor=False), height=height)),
        key_bindings=kb,
        style=menu_style,
        full_screen=False,
    )

    try:
        result = app.run()
        _dbg(f"[step_menu] app.run() returned: {result!r}")
        return result
    except KeyboardInterrupt:
        sys.exit(0)


# ══════════════════════════════════════════════════════════════════
#  STEP 0 — PRE-FLIGHT CHECKS
# ══════════════════════════════════════════════════════════════════

def detect_os() -> tuple[str, bool]:
    """Returns (os_type, is_wsl)."""
    system = platform.system().lower()
    is_wsl = False

    if system == "linux":
        # Check for WSL
        try:
            with open("/proc/version", "r") as f:
                ver = f.read().lower()
                if "microsoft" in ver or "wsl" in ver:
                    is_wsl = True
        except FileNotFoundError:
            pass
        return ("wsl" if is_wsl else "linux", is_wsl)
    elif system == "darwin":
        return ("macos", False)
    elif system == "windows":
        return ("windows", False)
    return ("unknown", False)


def check_command(cmd: str) -> tuple[bool, str]:
    """Check if a command exists and return its version."""
    try:
        result = subprocess.run(
            [cmd, "--version"], capture_output=True, text=True, timeout=10
        )
        version = result.stdout.strip().split("\n")[0] if result.stdout else ""
        if not version:
            version = result.stderr.strip().split("\n")[0] if result.stderr else ""
        # Truncate long version strings
        if len(version) > 60:
            version = version[:57] + "..."
        return (True, version)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (False, "")


def check_docker_running() -> tuple[bool, str]:
    """Check if Docker daemon is running. Returns (ok, error_detail)."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return (True, "")
        stderr = result.stderr.strip()
        # Grab first meaningful line of stderr
        detail = stderr.split("\n")[0][:120] if stderr else f"exit code {result.returncode}"
        return (False, detail)
    except FileNotFoundError:
        return (False, "docker command not found in PATH")
    except subprocess.TimeoutExpired:
        return (False, "docker ps timed out after 30s — daemon may be starting")


def check_wsl_metadata() -> bool:
    """Check if WSL has metadata mount option enabled."""
    try:
        with open("/etc/wsl.conf", "r") as f:
            content = f.read().lower()
            return "metadata" in content
    except FileNotFoundError:
        return False


def fix_wsl_metadata() -> bool:
    """Auto-fix WSL metadata by appending [automount] to /etc/wsl.conf."""
    conf_path = "/etc/wsl.conf"
    automount_block = "\n[automount]\noptions = \"metadata\"\n"
    try:
        existing = ""
        try:
            with open(conf_path, "r") as f:
                existing = f.read()
        except FileNotFoundError:
            pass

        # If [automount] section exists, append the option under it
        if "[automount]" in existing.lower():
            # Already has section — inject options line after the header
            import re as _re
            patched = _re.sub(
                r'(\[automount\])',
                r'\1\noptions = "metadata"',
                existing,
                count=1,
                flags=_re.IGNORECASE,
            )
            result = subprocess.run(
                ["sudo", "tee", conf_path],
                input=patched, capture_output=True, text=True,
            )
        else:
            # No [automount] section — append the whole block
            result = subprocess.run(
                ["sudo", "tee", "-a", conf_path],
                input=automount_block, capture_output=True, text=True,
            )
        return result.returncode == 0
    except Exception:
        return False


def run_preflight(config: SetupConfig) -> bool:
    show_step_header(1, TOTAL_STEPS, "Pre-Flight Checks", "Verifying required tools are installed")

    os_type, is_wsl = detect_os()
    config.os_type = os_type
    config.is_wsl = is_wsl

    console.print(f"  [bold]Detected OS:[/] {os_type.upper()}" +
                  (" (Windows Subsystem for Linux)" if is_wsl else ""))
    console.print()

    all_ok = True
    critical_missing = []

    # ── Required tools ──
    console.print("  [bold underline]Required[/]")
    console.print()

    # Docker
    docker_ok, docker_ver = check_command("docker")
    docker_running, docker_err = check_docker_running() if docker_ok else (False, "")
    show_check("Docker", docker_ok, docker_ver)
    if docker_ok and not docker_running:
        console.print("    [yellow]⚠  Docker is installed but not responding.[/]")
        if docker_err:
            console.print(f"    [dim]Reason: {docker_err}[/]")
        all_ok = False
        critical_missing.append("Docker (not running)")
    elif not docker_ok:
        all_ok = False
        critical_missing.append("Docker")

    # Git
    git_ok, git_ver = check_command("git")
    show_check("Git", git_ok, git_ver)
    if not git_ok:
        all_ok = False
        critical_missing.append("Git")

    # Bash
    bash_ok, bash_ver = check_command("bash")
    show_check("Bash", bash_ok, bash_ver)
    if not bash_ok:
        all_ok = False
        critical_missing.append("Bash")

    console.print()
    console.print("  [bold underline]Optional (installed inside Docker image)[/]")
    console.print()

    # These are informational — they get installed in the Docker image
    node_ok, node_ver = check_command("node")
    show_check("Node.js (host)", node_ok, node_ver if node_ok else "Not needed on host")

    python_ok, python_ver = check_command("python3")
    show_check("Python 3 (host)", python_ok, python_ver if python_ok else "Not needed on host")

    gh_ok, gh_ver = check_command("gh")
    show_check("GitHub CLI (host)", gh_ok, gh_ver if gh_ok else "Not needed on host")

    # ── WSL-specific checks ──
    if is_wsl:
        console.print()
        console.print("  [bold underline]WSL-Specific[/]")
        console.print()
        wsl_meta = check_wsl_metadata()
        show_check("WSL metadata mount", wsl_meta,
                    "chmod will work on /mnt/c" if wsl_meta else "Needs fix")
        if not wsl_meta:
            console.print()
            console.print("    [yellow]chmod won't work on Windows filesystem files without metadata.[/]")
            console.print()
            auto_fix = questionary.confirm(
                "  Auto-fix /etc/wsl.conf? (requires sudo)",
                default=True,
            ).ask()
            if auto_fix:
                if fix_wsl_metadata():
                    console.print("    [green]✓ Updated /etc/wsl.conf with metadata option.[/]")
                    console.print("    [cyan]Restart WSL to apply: close this terminal, then in PowerShell run:[/]")
                    console.print("      [bold cyan]wsl --shutdown[/]")
                    console.print("    [cyan]Then reopen your WSL terminal and re-run setup.[/]")
                else:
                    console.print("    [red]✗ Could not update /etc/wsl.conf. Apply manually:[/]")
                    show_info_box("Manual Fix", textwrap.dedent("""
                        Add to [bold]/etc/wsl.conf[/]:

                          [cyan][automount]
                          options = "metadata"[/]

                        Then restart WSL from PowerShell:
                          [cyan]wsl --shutdown[/]
                    """).strip(), style="yellow")
            else:
                show_info_box("Manual Fix", textwrap.dedent("""
                    Add to [bold]/etc/wsl.conf[/]:

                      [cyan][automount]
                      options = "metadata"[/]

                    Then restart WSL from PowerShell:
                      [cyan]wsl --shutdown[/]
                """).strip(), style="yellow")

    # ── Summary ──
    console.print()
    if all_ok:
        show_info_box("All Clear", "[bold green]All required tools are installed and running.[/]\nReady to proceed with setup.", style="green")
        pause()
        return True

    # If the only issue is Docker not running (installed but stopped), offer retry
    only_docker_not_running = (docker_ok and not docker_running
                               and critical_missing == ["Docker (not running)"])

    if only_docker_not_running:
        show_info_box("Docker Not Running",
                      "[bold yellow]Docker is installed but not running.[/]\n\n"
                      "Start Docker Desktop, then press [bold]R[/] to re-check.",
                      style="yellow")
        while True:
            choice = Prompt.ask(
                "  [bold][R][/]etry  /  [bold][Q][/]uit",
                choices=["r", "q", "R", "Q"],
                default="r"
            ).lower()
            if choice == "q":
                return False
            # Re-check Docker
            console.print("\n  [dim]Checking Docker...[/]")
            running, err = check_docker_running()
            if running:
                console.print("  [bold green]✔[/] Docker is now running!")
                console.print()
                show_info_box("All Clear", "[bold green]All required tools are installed and running.[/]\nReady to proceed with setup.", style="green")
                pause()
                return True
            else:
                console.print("  [bold red]✘[/] Docker still not responding.")
                if err:
                    console.print(f"    [dim]Reason: {err}[/]")
                console.print()
    else:
        missing_str = ", ".join(critical_missing)
        show_info_box("Missing Requirements",
                      f"[bold red]Cannot proceed.[/] Missing: [bold]{missing_str}[/]\n\n"
                      "Install the missing tools and run this wizard again.",
                      style="red")
        pause()
        return False


# ══════════════════════════════════════════════════════════════════
#  STEP 1 — INSTALL DIRECTORY
# ══════════════════════════════════════════════════════════════════

def _get_windows_home_via_wsl() -> str:
    """Resolve the real Windows user profile path from inside WSL."""
    try:
        result = subprocess.run(
            ["cmd.exe", "/C", "echo", "%USERPROFILE%"],
            capture_output=True, text=True, timeout=10,
        )
        win_path = result.stdout.strip()
        if win_path and ":" in win_path:
            # Convert e.g. C:\Users\pbrne → /mnt/c/Users/pbrne
            drive = win_path[0].lower()
            rest = win_path[2:].replace("\\", "/")
            return f"/mnt/{drive}{rest}"
    except Exception:
        pass
    return ""


def step_install_dir(config: SetupConfig, came_from="next"):
    if config.is_wsl:
        # Resolve actual Windows home — $USER is the WSL username which
        # may differ from the Windows username (causes PermissionError)
        win_home = _get_windows_home_via_wsl()
        if win_home:
            default_dir = f"{win_home}/alcatraz"
        else:
            # Fallback: best guess from WSL $USER
            win_user = os.environ.get("USER", os.environ.get("LOGNAME", ""))
            default_dir = f"/mnt/c/Users/{win_user}/alcatraz"
    else:
        default_dir = os.path.expanduser("~/alcatraz")
    first_iter = True

    while True:
        clear_screen()
        show_banner()
        show_step_header(2, TOTAL_STEPS, "Installation Directory",
                         "Where to create the alcatraz setup files")

        # On WSL, hint that ~/... paths live in the WSL filesystem, not the Windows drive
        if config.is_wsl:
            console.print("  [dim]Tip: On WSL, paths under ~ (e.g. ~/alcatraz) are stored in the WSL\n"
                          "  filesystem, not your Windows drive. To keep files on the Windows drive,\n"
                          "  use /mnt/c/Users/YourName/... instead.[/]")
            console.print()

        # Show current value
        current = config.install_dir or default_dir
        status = "[green]✔[/]" if config.is_step_complete(0) else "[dim]○[/]"
        console.print(f"  {status} [bold]Directory:[/] {current}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([("Edit directory", "edit")], initial_nav=nav)

        if result == "back":
            return result
        if result == "next":
            # Accept default if not yet edited
            if not config.install_dir:
                config.install_dir = default_dir
            config.mark_complete(0)
            return result

        # result == "edit"
        dir_input = questionary.text(
            "Installation directory:",
            default=current,
            style=q_style,
        ).ask()

        if dir_input is None:
            sys.exit(0)

        expanded = os.path.expanduser(dir_input)

        home_dir = os.path.expanduser("~")
        abs_install = os.path.abspath(expanded)
        valid = True

        # Block system directories — hard reject (not just a warning)
        normalized = abs_install.replace('\\', '/').rstrip('/')
        _forbidden = ('/etc', '/usr', '/bin', '/sbin', '/var', '/root',
                      '/opt', '/sys', '/proc', '/dev', '/boot', '/lib')
        for prefix in _forbidden:
            if normalized == prefix or normalized.startswith(prefix + '/'):
                console.print(f"  [red]✗  Cannot install to system directory: {abs_install}[/]")
                console.print(f"  [dim]Choose a path under your home directory instead.[/]")
                valid = False
                break

        # Warn if install path is outside home directory
        if valid and not abs_install.startswith(home_dir):
            console.print(f"  [yellow]⚠  Warning: directory is outside your home folder ({home_dir}).[/]")
            proceed = questionary.confirm(
                "Continue with this path?",
                default=False,
                style=q_style,
            ).ask()
            if proceed is None:
                sys.exit(0)
            if not proceed:
                valid = False

        if valid and os.path.exists(expanded):
            overwrite = questionary.confirm(
                f"Directory '{expanded}' already exists. Overwrite files?",
                default=False,
                style=q_style,
            ).ask()
            if overwrite is None:
                sys.exit(0)
            if not overwrite:
                valid = False

        if valid:
            config.install_dir = expanded
            config.mark_complete(0)


# ══════════════════════════════════════════════════════════════════
#  STEP 2 — PROFILE SELECTION
# ══════════════════════════════════════════════════════════════════

def step_profile(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(3, TOTAL_STEPS, "Setup Profile",
                         "Choose a pre-configured profile or customise everything")

        term_w = console.width
        table_w = min(term_w - 6, 120)
        show_best_for = term_w >= 90

        table = Table(box=box.ROUNDED, border_style="cyan", show_header=True,
                      header_style="bold white",
                      padding=(1, 1 if not show_best_for else 2),
                      width=table_w)
        table.add_column("Profile", style="bold cyan", no_wrap=True)
        table.add_column("Image", no_wrap=True)
        table.add_column("Includes")
        if show_best_for:
            table.add_column("Best For", style="dim")

        star = lambda p: "⭐ " if config.profile == p else "   "
        rows = [
            (f"{star('recommended')}Recommended", "~4–5 GB",
             "Core tools, GitHub CLI, Cloud CLIs, Infra, Browser, DB clients",
             "Most web/backend teams"),
            (f"{star('minimal')}Minimal", "~1.5–2 GB",
             "Core tools, GitHub CLI only",
             "Quick start, limited disk"),
            (f"{star('full')}Full", "~7–8 GB",
             "Everything + ML/Data Science packages (CPU)",
             "ML engineers, data teams"),
            (f"{star('custom')}Custom", "Varies",
             "You pick each component",
             "Specific requirements"),
        ]
        for row in rows:
            table.add_row(*(row if show_best_for else row[:3]))

        console.print(Padding(table, (0, 2)))
        console.print()

        # Show current selection
        status = "[green]✔[/]" if config.is_step_complete(1) else "[dim]○[/]"
        console.print(f"  {status} [bold]Profile:[/] {config.profile}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([("Change profile", "edit")], initial_nav=nav)
        _dbg(f"[step_profile] step_menu returned: {result!r}, type={type(result).__name__}")

        if result == "back":
            _dbg(f"[step_profile] taking BACK branch")
            return result
        if result == "next":
            _dbg(f"[step_profile] taking NEXT branch")
            config.mark_complete(1)
            return result

        _dbg(f"[step_profile] falling through to EDIT (result was {result!r})")
        # result == "edit"
        sel = lambda p: "⭐" if config.profile == p else "  "
        profile = questionary.select(
            "Select a profile:",
            choices=[
                questionary.Choice(f"{sel('recommended')} Recommended  — Core + Cloud + Infra + Browser", value="recommended"),
                questionary.Choice(f"{sel('minimal')} Minimal      — Core + GitHub CLI only", value="minimal"),
                questionary.Choice(f"{sel('full')} Full         — Everything including ML packages", value="full"),
                questionary.Choice(f"{sel('custom')} Custom       — Choose each component", value="custom"),
            ],
            style=q_style,
        ).ask()

        if profile is None:
            sys.exit(0)

        config.profile = profile

        if profile == "recommended":
            config.include_cloud_clis = True
            config.include_infra_tools = True
            config.include_ml_packages = False
            config.include_browser = True
            config.include_docker_cli = True
            config.include_github_cli = True
            config.include_db_clients = True
        elif profile == "minimal":
            config.include_cloud_clis = False
            config.include_infra_tools = False
            config.include_ml_packages = False
            config.include_browser = False
            config.include_docker_cli = False
            config.include_github_cli = True
            config.include_db_clients = False
        elif profile == "full":
            config.include_cloud_clis = True
            config.include_infra_tools = True
            config.include_ml_packages = True
            config.include_browser = True
            config.include_docker_cli = True
            config.include_github_cli = True
            config.include_db_clients = True
        elif profile == "custom":
            step_custom_components(config)

        config.mark_complete(1)


def step_custom_components(config: SetupConfig):
    console.print()
    console.print("  [bold]Select components to include in the Docker image:[/]")
    console.print()

    components = questionary.checkbox(
        "Components (Space to toggle, Enter to confirm):",
        choices=[
            questionary.Choice("GitHub CLI (gh) — open PRs, check CI", value="github_cli", checked=config.include_github_cli),
            questionary.Choice("Docker CLI — build/run containers from inside", value="docker_cli", checked=config.include_docker_cli),
            questionary.Choice("Cloud CLIs — AWS, GCP, Azure", value="cloud_clis", checked=config.include_cloud_clis),
            questionary.Choice("Infrastructure — Terraform, kubectl", value="infra_tools", checked=config.include_infra_tools),
            questionary.Choice("Database clients — SQLite, PostgreSQL, MySQL, Redis", value="db_clients", checked=config.include_db_clients),
            questionary.Choice("Browser — Chrome + Playwright (for MCP / testing)", value="browser", checked=config.include_browser),
            questionary.Choice("ML/Data Science — PyTorch (CPU), NumPy, Pandas, Jupyter", value="ml_packages", checked=config.include_ml_packages),
        ],
        style=q_style,
    ).ask()

    if components is None:
        sys.exit(0)

    config.include_github_cli = "github_cli" in components
    config.include_docker_cli = "docker_cli" in components
    config.include_cloud_clis = "cloud_clis" in components
    config.include_infra_tools = "infra_tools" in components
    config.include_db_clients = "db_clients" in components
    config.include_browser = "browser" in components
    config.include_ml_packages = "ml_packages" in components
    return "next"


# ══════════════════════════════════════════════════════════════════
#  STEP 3 — GIT GUARDIAN CONFIG
# ══════════════════════════════════════════════════════════════════

def _edit_git_guardian(config: SetupConfig):
    """Collect all Git Guardian inputs."""
    # Protected branches
    branches_str = questionary.text(
        "Protected branch names (comma-separated):",
        default=", ".join(config.protected_branches),
        style=q_style,
    ).ask()

    if branches_str is None:
        sys.exit(0)

    raw_branches = [b.strip() for b in branches_str.split(",") if b.strip()]
    valid_branches = []
    for b in raw_branches:
        if validate_branch_name(b):
            valid_branches.append(b)
        else:
            console.print(f"  [yellow]⚠  Skipping invalid branch name:[/] [bold]{b}[/] "
                          "(only alphanumeric, '.', '_', '/', '-', '*' allowed)")
    if not valid_branches:
        console.print("  [yellow]No valid branch names — using defaults.[/]")
        valid_branches = ["main", "master", "develop", "production", "release"]
    config.protected_branches = valid_branches

    # Confirm all pushes?
    result = questionary.confirm(
        "Require confirmation for ALL pushes (not just protected branches)?",
        default=config.guardian_confirm_all_pushes,
        style=q_style,
    ).ask()
    if result is None:
        sys.exit(0)
    config.guardian_confirm_all_pushes = result

    # Auto-allow claude/ branches
    if not config.guardian_confirm_all_pushes:
        result = questionary.confirm(
            "Auto-allow pushes to branches prefixed 'claude/'?",
            default=config.guardian_auto_allow_claude_branches,
            style=q_style,
        ).ask()
        if result is None:
            sys.exit(0)
        config.guardian_auto_allow_claude_branches = result

    config.mark_complete(2)


def step_git_guardian(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(4, TOTAL_STEPS, "Git Guardian Configuration",
                         "Configure the safety wrapper around git commands")

        show_info_box("What is Git Guardian?", textwrap.dedent("""
            The Git Guardian sits between Claude and the real [bold]git[/] binary.
            Safe commands ([cyan]add, commit, diff, log[/]) pass through silently.
            Dangerous commands ([red]force push, branch delete, hard reset[/]) pause
            and ask [bold]you[/] for confirmation in the terminal.
        """).strip())

        console.print()

        # Show current values
        status = "[green]✔[/]" if config.is_step_complete(2) else "[dim]○[/]"
        console.print(f"  {status} [bold]Protected branches:[/] {', '.join(config.protected_branches)}")
        confirm_label = "All pushes" if config.guardian_confirm_all_pushes else "Protected only"
        console.print(f"    [bold]Push confirmation:[/] {confirm_label}")
        if not config.guardian_confirm_all_pushes:
            auto_label = "Yes" if config.guardian_auto_allow_claude_branches else "No"
            console.print(f"    [bold]Auto-allow claude/ branches:[/] {auto_label}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([("Edit Git Guardian settings", "edit")], initial_nav=nav)

        if result == "back":
            return result
        if result == "next":
            config.mark_complete(2)
            return result

        _edit_git_guardian(config)


# ══════════════════════════════════════════════════════════════════
#  STEP 4 — NETWORK & PORTS
# ══════════════════════════════════════════════════════════════════

def _edit_network(config: SetupConfig):
    """Collect all network & port inputs."""
    result = questionary.select(
        "Default network mode:",
        choices=[
            questionary.Choice("bridge (recommended) — Claude has internet access", value="bridge"),
            questionary.Choice("none — Fully offline, you push from host", value="none"),
        ],
        style=q_style,
    ).ask()

    if result is None:
        sys.exit(0)
    config.default_network = result

    console.print()
    console.print("  [bold]Port forwarding mode[/]")
    console.print()

    port_mode = questionary.select(
        "Port mapping strategy:",
        choices=[
            questionary.Choice(
                "deterministic (recommended) — Hash-based ports, parallel safe",
                value="deterministic"),
            questionary.Choice(
                "fixed — 1:1 mapping (3000:3000), single container only",
                value="fixed"),
            questionary.Choice(
                "noports — No port forwarding at all",
                value="noports"),
        ],
        default="deterministic",
        style=q_style,
    ).ask()

    if port_mode is None:
        sys.exit(0)
    config.port_mode = port_mode

    if port_mode != "noports":
        console.print()
        console.print("  [bold]Container ports to forward[/] (bound to 127.0.0.1)")
        console.print("  [dim]Default: 3000, 3001, 5173, 8080[/]")
        if port_mode == "deterministic":
            console.print("  [dim]Each project gets unique host ports via hashing — no conflicts.[/]")
        console.print()

        add_ports = questionary.confirm(
            "Add additional ports beyond the defaults?",
            default=False,
            style=q_style,
        ).ask()
        if add_ports is None:
            sys.exit(0)

        if add_ports:
            extra = questionary.text(
                "Additional ports (comma-separated):",
                default=", ".join(config.custom_ports) if config.custom_ports else "",
                style=q_style,
            ).ask()
            if extra is None:
                sys.exit(0)
            if extra:
                valid_ports = []
                for p in extra.split(","):
                    p = p.strip()
                    if not p:
                        continue
                    if validate_port(p):
                        valid_ports.append(p)
                    else:
                        console.print(f"  [yellow]⚠  Skipping invalid port:[/] {p} (must be 1-65535)")
                config.custom_ports = valid_ports

    config.mark_complete(3)


def step_network(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(5, TOTAL_STEPS, "Network & Ports",
                         "Configure container networking and port forwarding")

        # Show current values
        status = "[green]✔[/]" if config.is_step_complete(3) else "[dim]○[/]"
        console.print(f"  {status} [bold]Network mode:[/] {config.default_network}")
        port_mode_labels = {
            "deterministic": "deterministic (parallel safe)",
            "fixed": "fixed 1:1 (single container)",
            "noports": "none (no port forwarding)",
        }
        console.print(f"    [bold]Port mode:[/] {port_mode_labels.get(config.port_mode, config.port_mode)}")
        if config.port_mode != "noports":
            all_ports = config.ports + config.custom_ports
            console.print(f"    [bold]Container ports:[/] {', '.join(str(p) for p in all_ports)}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([("Edit network settings", "edit")], initial_nav=nav)

        if result == "back":
            return result
        if result == "next":
            config.mark_complete(3)
            return result

        _edit_network(config)


# ══════════════════════════════════════════════════════════════════
#  STEP 5 — SECURITY OPTIONS
# ══════════════════════════════════════════════════════════════════

def _edit_security(config: SetupConfig):
    """Collect all security & auth inputs."""
    security_opts = questionary.checkbox(
        "Enable security layers (Space to toggle):",
        choices=[
            questionary.Choice("Permission deny list (recommended)", value="deny_list", checked=config.enable_deny_list),
            questionary.Choice("PreToolUse hook (recommended)", value="pretool_hook", checked=config.enable_pretool_hook),
            questionary.Choice("Session timeout", value="session_timeout", checked=config.enable_session_timeout),
            questionary.Choice("Read-only hooks mount", value="readonly_hooks", checked=config.enable_readonly_hooks),
            questionary.Choice("Resource limits (memory & CPU cap)", value="resource_limits", checked=config.enable_resource_limits),
        ],
        style=q_style,
    ).ask()

    if security_opts is None:
        sys.exit(0)

    config.enable_deny_list = "deny_list" in security_opts
    config.enable_pretool_hook = "pretool_hook" in security_opts
    config.enable_session_timeout = "session_timeout" in security_opts
    config.enable_readonly_hooks = "readonly_hooks" in security_opts
    config.enable_resource_limits = "resource_limits" in security_opts

    if config.enable_session_timeout:
        timeout_str = questionary.text(
            "Session timeout (hours):",
            default=str(config.session_timeout_hours),
            style=q_style,
        ).ask()
        if timeout_str is None:
            sys.exit(0)
        try:
            val = int(timeout_str)
            config.session_timeout_hours = val if val > 0 else 4
        except (ValueError, TypeError):
            console.print("  [yellow]Invalid timeout — using default (4 hours).[/]")
            config.session_timeout_hours = 4

    if config.enable_resource_limits:
        mem_input = questionary.text(
            "Memory limit (e.g., 8g, 16g):",
            default=config.resource_memory,
            style=q_style,
        ).ask()
        if mem_input is None:
            sys.exit(0)
        if validate_memory_limit(mem_input):
            config.resource_memory = mem_input
        else:
            console.print(f"  [yellow]Invalid memory format '{mem_input}' — using default (8g).[/]")
            config.resource_memory = "8g"

        cpu_str = questionary.text(
            "CPU limit (number of cores):",
            default=str(config.resource_cpus),
            style=q_style,
        ).ask()
        if cpu_str is None:
            sys.exit(0)
        try:
            val = int(cpu_str)
            if val < 1 or val > 32:
                console.print(f"  [yellow]CPU count must be 1-32 — using default (4).[/]")
                config.resource_cpus = 4
            else:
                config.resource_cpus = val
        except (ValueError, TypeError):
            console.print("  [yellow]Invalid CPU count — using default (4).[/]")
            config.resource_cpus = 4

    # PAT type
    console.print()
    result = questionary.select(
        "GitHub PAT type you'll use:",
        choices=[
            questionary.Choice("Fine-grained (recommended) — scoped to specific repos", value="fine-grained"),
            questionary.Choice("Classic — broader access, use if org hasn't enabled fine-grained", value="classic"),
        ],
        style=q_style,
    ).ask()
    if result is None:
        sys.exit(0)
    config.pat_type = result

    # Auth method
    console.print()
    result = questionary.select(
        "Claude Code authentication method:",
        choices=[
            questionary.Choice("OAuth login (Claude Pro/Max/Team/Enterprise)", value="oauth"),
            questionary.Choice("Anthropic API key", value="api_key"),
        ],
        style=q_style,
    ).ask()
    if result is None:
        sys.exit(0)
    config.auth_method = result

    config.mark_complete(4)


def step_security(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(6, TOTAL_STEPS, "Security & Safety Layers",
                         "Configure additional protection layers")

        # Show current values
        status = "[green]✔[/]" if config.is_step_complete(4) else "[dim]○[/]"
        layers = []
        if config.enable_deny_list:
            layers.append("Deny list")
        if config.enable_pretool_hook:
            layers.append("PreToolUse hook")
        if config.enable_session_timeout:
            layers.append(f"Timeout ({config.session_timeout_hours}h)")
        if config.enable_readonly_hooks:
            layers.append("Read-only hooks")
        if config.enable_resource_limits:
            layers.append(f"Resources ({config.resource_memory}, {config.resource_cpus} CPU)")
        console.print(f"  {status} [bold]Security layers:[/] {', '.join(layers) if layers else 'None'}")
        console.print(f"    [bold]PAT type:[/] {config.pat_type}")
        console.print(f"    [bold]Auth method:[/] {config.auth_method}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([("Edit security & auth settings", "edit")], initial_nav=nav)

        if result == "back":
            return result
        if result == "next":
            config.mark_complete(4)
            return result

        _edit_security(config)


# ══════════════════════════════════════════════════════════════════
#  FILE GENERATORS
# ══════════════════════════════════════════════════════════════════

def generate_git_guardian(config: SetupConfig) -> str:
    # Quote each branch name to prevent glob interpretation in bash case statements.
    # Single quotes disable wildcards in case patterns. The branch regex guarantees
    # no single quotes in names, so this is safe.
    branches = "|".join(f"'{b}'" for b in config.protected_branches)

    # Build the push case
    push_extra = ""
    if config.guardian_auto_allow_claude_branches and not config.guardian_confirm_all_pushes:
        push_extra = """
        elif args_match '^claude/' "${sub_args[@]}"; then
            echo -e "${GREEN}● Git Guardian: push to claude/ branch allowed${NC}" >&2
            log "PASSED: claude/ branch push"
"""

    push_else = ""
    if config.guardian_confirm_all_pushes:
        push_else = """        else
            confirm "MEDIUM" "Push detected. Confirm to proceed." "$@" || exit 1"""
    else:
        push_else = """        else
            echo -e "${GREEN}● Git Guardian: push allowed (feature branch)${NC}" >&2
            log "PASSED: feature branch push"
"""

    return textwrap.dedent(f'''\
#!/bin/bash
# git-guardian.sh — intercepts dangerous git commands and asks for confirmation
# Generated by Alcatraz Setup Wizard v{VERSION}

REAL_GIT="/usr/bin/git"

# ─── Logging ───
LOG_FILE="/tmp/git-guardian.log"
log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" >> "$LOG_FILE"
}}
log "COMMAND: git $*"

# ─── Colour codes ───
RED='\\033[0;31m'
YELLOW='\\033[1;33m'
GREEN='\\033[0;32m'
CYAN='\\033[0;36m'
BOLD='\\033[1m'
NC='\\033[0m'

# ─── Helper: check if any argument matches a pattern ───
args_match() {{
    local pattern="$1"
    shift
    for arg in "$@"; do
        if [[ "$arg" =~ $pattern ]]; then
            return 0
        fi
    done
    return 1
}}

# ─── Helper: check if a specific branch name appears in args ───
has_protected_branch() {{
    for arg in "$@"; do
        [[ "$arg" == -* ]] && continue
        case "$arg" in
            {branches}) return 0 ;;
        esac
    done
    return 1
}}

# ─── Helper: ask the user ───
confirm() {{
    local risk_level="$1"
    local description="$2"
    local colour="$YELLOW"
    [[ "$risk_level" == "HIGH" ]] && colour="$RED"

    local label="GIT GUARDIAN — ${{risk_level}} RISK ACTION DETECTED"
    local width=56

    echo "" >&2
    printf "${{colour}}╔%${{width}}s╗${{NC}}\\n" "" | tr ' ' '═' >&2
    printf "${{colour}}║${{NC}}  %-$(( width - 2 ))s${{colour}}║${{NC}}\\n" "$label" >&2
    printf "${{colour}}╠%${{width}}s╣${{NC}}\\n" "" | tr ' ' '═' >&2
    printf "${{colour}}║${{NC}}  %-$(( width - 2 ))s${{colour}}║${{NC}}\\n" "$description" >&2
    printf "${{colour}}║${{NC}}  %-$(( width - 2 ))s${{colour}}║${{NC}}\\n" "" >&2
    printf "${{colour}}║${{NC}}  %-$(( width - 2 ))s${{colour}}║${{NC}}\\n" "Full command: git $*" >&2
    printf "${{colour}}╚%${{width}}s╝${{NC}}\\n" "" | tr ' ' '═' >&2
    echo "" >&2

    if ! [ -t 0 ] && ! [ -e /dev/tty ]; then
        echo -e "${{RED}}✗ Git Guardian: no TTY — blocking action (safety default)${{NC}}" >&2
        log "BLOCKED (no TTY): $risk_level — $description"
        return 1
    fi

    read -p "$(echo -e "${{colour}}Allow this? [y/N]: ${{NC}}")" answer < /dev/tty
    case "$answer" in
        [yY]|[yY][eE][sS])
            log "ALLOWED (user approved): $risk_level — $description"
            return 0
            ;;
        *)
            echo -e "${{RED}}✗ Blocked by Git Guardian${{NC}}" >&2
            log "BLOCKED (user denied): $risk_level — $description"
            return 1
            ;;
    esac
}}

# ─── Detect the subcommand (skip global flags) ───
subcommand=""
sub_args=()
skip_next=false
for arg in "$@"; do
    if $skip_next; then
        skip_next=false
        continue
    fi
    if [[ -z "$subcommand" ]]; then
        case "$arg" in
            -C|-c|--git-dir|--work-tree|--namespace) skip_next=true; continue ;;
            -*) continue ;;
            *)  subcommand="$arg" ;;
        esac
    else
        sub_args+=("$arg")
    fi
done

# ─── Rules ───
case "$subcommand" in

    push)
        if args_match '^(--force|-f)$' "${{sub_args[@]}}"; then
            confirm "HIGH" "Force push detected. This rewrites remote history." "$@" || exit 1
        elif args_match '^--force-with-lease' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Force-with-lease push. Safer, but still rewrites history." "$@" || exit 1
        elif args_match '^(--delete|-d)$' "${{sub_args[@]}}" || args_match '^:.' "${{sub_args[@]}}"; then
            confirm "HIGH" "Remote branch deletion detected." "$@" || exit 1
        elif has_protected_branch "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Push to a protected branch name detected." "$@" || exit 1
{push_extra}{push_else}
        fi
        ;;

    branch)
        if args_match '^(-d|-D|--delete)$' "${{sub_args[@]}}"; then
            if has_protected_branch "${{sub_args[@]}}"; then
                echo -e "${{RED}}✗ Git Guardian: BLOCKED — deleting a protected branch is not allowed${{NC}}" >&2
                log "HARD-BLOCKED: branch delete on protected branch — git $*"
                exit 1
            elif args_match '^-D$' "${{sub_args[@]}}"; then
                confirm "MEDIUM" "Force-deleting a local branch." "$@" || exit 1
            fi
        fi
        ;;

    reset)
        if args_match '^--hard$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Hard reset. Uncommitted changes will be lost." "$@" || exit 1
        fi
        ;;

    clean)
        if args_match '^(-f|--force)$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Forced clean. Untracked files will be deleted." "$@" || exit 1
        fi
        ;;

    rebase)
        if args_match '^--onto$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Rebase --onto detected. This rewrites commit history." "$@" || exit 1
        elif args_match '^(-i|--interactive)$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Interactive rebase. This rewrites commit history." "$@" || exit 1
        fi
        ;;

    commit)
        if args_match '^--amend$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Amending the last commit. Risky if already pushed." "$@" || exit 1
        fi
        ;;

    checkout)
        if has_protected_branch "${{sub_args[@]}}"; then
            echo -e "${{CYAN}}● Git Guardian: switching to a primary branch${{NC}}" >&2
            log "INFO: checkout to primary branch"
        elif args_match '^--$' "${{sub_args[@]}}" || [[ "${{sub_args[0]}}" == "." ]]; then
            confirm "MEDIUM" "Discarding uncommitted changes to working tree." "$@" || exit 1
        fi
        ;;

    switch)
        if has_protected_branch "${{sub_args[@]}}"; then
            echo -e "${{CYAN}}● Git Guardian: switching to a primary branch${{NC}}" >&2
            log "INFO: switch to primary branch"
        fi
        ;;

    restore)
        if args_match '^\\.$' "${{sub_args[@]}}"; then
            confirm "MEDIUM" "Restoring all files. Uncommitted changes may be lost." "$@" || exit 1
        fi
        ;;

    stash)
        if [[ "${{sub_args[0]}}" == "drop" ]]; then
            confirm "MEDIUM" "Dropping a stash entry. This cannot be undone easily." "$@" || exit 1
        elif [[ "${{sub_args[0]}}" == "clear" ]]; then
            confirm "HIGH" "Clearing ALL stashes. This cannot be undone." "$@" || exit 1
        fi
        ;;

esac

exec "$REAL_GIT" "$@"
''')


def generate_dockerfile(config: SetupConfig) -> str:
    sections = []

    # Base
    sections.append(textwrap.dedent("""\
        FROM node:20-bookworm-slim

        # ============================================================
        # 1. System dependencies
        # ============================================================
        RUN apt-get update && apt-get install -y --no-install-recommends \\
            git curl wget sudo ca-certificates gnupg openssh-client zip unzip \\
            build-essential pkg-config \\
            ripgrep fd-find jq tree htop \\
            python3 python3-pip python3-venv python3-dev \\"""))

    if config.include_db_clients:
        sections.append("""\
    sqlite3 libsqlite3-dev postgresql-client default-mysql-client redis-tools \\""")

    # Close the apt-get and add fd-find symlink
    sections.append("""\
    && rm -rf /var/lib/apt/lists/* \\
    && ln -s "$(which fdfind)" /usr/local/bin/fd \\
    && ln -sf /usr/bin/python3 /usr/bin/python""")

    # GitHub CLI
    if config.include_github_cli:
        sections.append(textwrap.dedent("""
        # ============================================================
        # 2. GitHub CLI
        # ============================================================
        RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
                | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \\
            && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \\
            && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
                | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \\
            && apt-get update && apt-get install -y gh \\
            && rm -rf /var/lib/apt/lists/*"""))

    # Docker CLI
    if config.include_docker_cli:
        sections.append(textwrap.dedent("""
        # ============================================================
        # 3. Docker CLI
        # ============================================================
        RUN curl -fsSL https://download.docker.com/linux/debian/gpg \\
                | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \\
            && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian bookworm stable" \\
                | tee /etc/apt/sources.list.d/docker.list > /dev/null \\
            && apt-get update && apt-get install -y docker-ce-cli \\
            && rm -rf /var/lib/apt/lists/*"""))

    # Cloud CLIs
    if config.include_cloud_clis:
        sections.append(textwrap.dedent("""
        # ============================================================
        # 4. Cloud CLIs — AWS, GCP, Azure
        # ============================================================
        RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "awscliv2.zip" \\
            && unzip -q awscliv2.zip && ./aws/install && rm -rf aws awscliv2.zip

        RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \\
                | tee /etc/apt/sources.list.d/google-cloud-sdk.list \\
            && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \\
                | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \\
            && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \\
            && rm -rf /var/lib/apt/lists/*

        RUN curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash"""))

    # Infrastructure
    if config.include_infra_tools:
        sections.append(textwrap.dedent("""
        # ============================================================
        # 5. Infrastructure — Terraform + kubectl
        # ============================================================
        RUN curl -fsSL https://apt.releases.hashicorp.com/gpg \\
                | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \\
            && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com bookworm main" \\
                | tee /etc/apt/sources.list.d/hashicorp.list \\
            && apt-get update && apt-get install -y terraform \\
            && rm -rf /var/lib/apt/lists/*

        RUN curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key \\
                | gpg --dearmor -o /usr/share/keyrings/kubernetes-apt-keyring.gpg \\
            && echo "deb [signed-by=/usr/share/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /" \\
                | tee /etc/apt/sources.list.d/kubernetes.list \\
            && apt-get update && apt-get install -y kubectl \\
            && rm -rf /var/lib/apt/lists/*"""))

    # Node package managers + Claude Code
    sections.append(textwrap.dedent("""
        # ============================================================
        # 6. Node.js package managers + Claude Code
        # ============================================================
        RUN npm install -g pnpm
        RUN npm install -g @anthropic-ai/claude-code \\
            && npm cache clean --force"""))

    # ML packages
    if config.include_ml_packages:
        sections.append(textwrap.dedent("""
        # ============================================================
        # 7. Python ML/Data Science packages (CPU-only)
        # ============================================================
        RUN pip install --break-system-packages --no-cache-dir \\
            torch --index-url https://download.pytorch.org/whl/cpu \\
            && pip install --break-system-packages --no-cache-dir \\
            numpy pandas scipy scikit-learn matplotlib jupyter notebook ipykernel"""))

    # User setup
    sections.append(textwrap.dedent("""
        # ============================================================
        # User setup
        # ============================================================
        ARG USER_UID=1000
        ARG USER_GID=1000
        RUN groupmod --gid $USER_GID node \\
            && usermod --uid $USER_UID --gid $USER_GID node \\
            && chown -R $USER_UID:$USER_GID /home/node

        # Minimal sudo: only credential helper operations (no apt-get, chmod, chown)
        RUN echo "node ALL=(ALL) NOPASSWD: /usr/bin/tee /root/.git-credentials, /bin/cat /root/.git-credentials, /usr/bin/cat /root/.git-credentials, /bin/chmod 600 /root/.git-credentials, /usr/bin/chmod 600 /root/.git-credentials" >> /etc/sudoers

        RUN git config --system push.default current \\
            && git config --system push.autoSetupRemote true

        # ── Git Guardian wrapper ──
        COPY git-guardian.sh /usr/local/bin/git
        RUN chmod +x /usr/local/bin/git"""))

    # Browser
    if config.include_browser:
        sections.append(textwrap.dedent("""
        # ============================================================
        # Browser — Google Chrome + Playwright
        # ============================================================
        RUN curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \\
            && apt-get update && apt-get install -y /tmp/chrome.deb \\
            && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

        ENV PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright
        ENV CHROME_PATH=/usr/bin/google-chrome-stable
        ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable"""))

    # Workdir + user switch
    sections.append(textwrap.dedent("""
        # Disable Claude Code auto-update — pinned at build time, avoids
        # permission errors when running as non-root 'node' user
        ENV DISABLE_AUTOUPDATER=1

        WORKDIR /workspace
        USER node"""))

    if config.include_browser:
        sections.append(textwrap.dedent("""
        RUN npx playwright install chromium"""))

    sections.append("""
        CMD ["bash"]""")

    return "\n".join(sections)


def generate_run_script(config: SetupConfig) -> str:
    all_ports = config.ports + [int(p) for p in config.custom_ports if p.isdigit()]
    ports_array = " ".join(str(p) for p in all_ports)

    # Build docker run flags — ports are now dynamic via ${PORT_ARGS[@]}
    docker_flags = []
    docker_flags.append('    --name "$CONTAINER_NAME"')
    docker_flags.append('    --network "$NETWORK_MODE"')
    docker_flags.append('    "${PORT_ARGS[@]}"')
    docker_flags.append('    "${PORT_ENV_ARGS[@]}"')
    docker_flags.append('    --cap-drop ALL')
    # Minimal capabilities — DAC_OVERRIDE and SYS_PTRACE removed (not needed for Claude Code)
    for cap in ['AUDIT_WRITE', 'CHOWN', 'FOWNER', 'FSETID',
                'KILL', 'NET_BIND_SERVICE', 'SETGID', 'SETUID']:
        docker_flags.append(f'    --cap-add {cap}')
    if config.enable_resource_limits:
        docker_flags.append(f'    --memory {config.resource_memory}')
        docker_flags.append(f'    --cpus {config.resource_cpus}')
    docker_flags.append('    -v "$PROJECT_DIR:/workspace"')
    docker_flags.append('    -v "$HOME/.claude:/home/node/.claude"')
    docker_flags.append('    -v "$HOME/.claude.json:/home/node/.claude.json"')
    if config.enable_readonly_hooks:
        docker_flags.append('    -v "$PROJECT_DIR/.claude/hooks:/workspace/.claude/hooks:ro"')
    docker_flags.append('    --env-file "$ENV_FILE"')
    docker_flags.append('    -e "HOST_GIT_NAME=$HOST_GIT_NAME"')
    docker_flags.append('    -e "HOST_GIT_EMAIL=$HOST_GIT_EMAIL"')
    docker_flags.append('    -e DISABLE_AUTOUPDATER=1')
    docker_flags.append('    alcatraz:latest')

    docker_flags_str = " \\\n".join(docker_flags)

    timeout_prefix = ""
    if config.enable_session_timeout:
        timeout_prefix = f"timeout {config.session_timeout_hours}h "

    api_key_block = ""
    if config.auth_method == "api_key":
        api_key_block = """
# ── Anthropic API key ──
ANTHROPIC_API_KEY_FILE="$HOME/.alcatraz-anthropic-key"
if [ -f "$ANTHROPIC_API_KEY_FILE" ]; then
    ANTHROPIC_KEY=$(cat "$ANTHROPIC_API_KEY_FILE" | tr -d '[:space:]')
    if echo "$ANTHROPIC_KEY" | grep -qE '^sk-ant-[a-zA-Z0-9_-]+$'; then
        printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_KEY" >> "$ENV_FILE"
    else
        echo "WARNING: Invalid Anthropic API key format in $ANTHROPIC_API_KEY_FILE — skipping"
    fi
fi
"""

    chrome_block = ""
    if config.include_browser:
        chrome_block = """
        # Launch headless Chrome for browser testing / chrome-devtools MCP
        if command -v google-chrome-stable &>/dev/null; then
            # Supervisor: auto-restart Chrome if it crashes (runs in background)
            (
                RESTART_DELAY=2
                MAX_DELAY=30
                while true; do
                    google-chrome-stable --no-sandbox --headless=new \\
                        --remote-debugging-port=9222 --disable-gpu \\
                        --disable-dev-shm-usage --no-first-run \\
                        about:blank 2>/tmp/chrome-error.log
                    echo \\"[\\$(date '+%H:%M:%S')] Chrome exited (\\$?), restarting in \\${RESTART_DELAY}s...\\" >&2
                    sleep \\$RESTART_DELAY
                    RESTART_DELAY=\\$((RESTART_DELAY * 2))
                    if [ \\$RESTART_DELAY -gt \\$MAX_DELAY ]; then
                        RESTART_DELAY=\\$MAX_DELAY
                    fi
                done
            ) &

            # Wait for Chrome to be ready (poll port 9222 instead of blind sleep)
            for i in \\$(seq 1 15); do
                if curl -sf http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
                    break
                fi
                sleep 1
            done
        fi
"""

    return textwrap.dedent(f'''\
#!/bin/bash
set -e
# run.sh — Launch Alcatraz
# Generated by Alcatraz Setup Wizard v{VERSION}

# Usage:
#   ./run.sh                                         # Current dir, deterministic ports (default)
#   ./run.sh /path/to/project                        # Specific dir, deterministic ports
#   ./run.sh fixed                                   # Current dir, fixed 1:1 ports (3000:3000 etc.)
#   ./run.sh none                                    # Current dir, offline (no network)
#   ./run.sh /path/to/project noports                # Specific dir, no port forwarding
#   ./run.sh none noports                            # Offline + no ports
#
# Port modes (order-independent, mix with any other args):
#   (default)  — Deterministic hash-based ports, parallel safe
#   fixed      — 1:1 mapping (3000:3000), only one container at a time
#   noports    — No port forwarding at all

# --- Configuration ---
GITHUB_TOKEN_FILE="$HOME/.alcatraz-token"
CONTAINER_PORTS=({ports_array})  # Container-side ports to forward

# --- Parse arguments ---
# Accepts args in any order: a directory path, a network mode (none/bridge),
# and a port mode (fixed/noports). Unrecognised args are treated as the project dir.
# Default: {config.port_mode} ports + {config.default_network} network + current directory.
PROJECT_DIR="."
NETWORK_MODE="{config.default_network}"
PORT_MODE="{config.port_mode}"

for arg in "$@"; do
    case "$arg" in
        fixed)       PORT_MODE="fixed" ;;
        noports)     PORT_MODE="noports" ;;
        none)        NETWORK_MODE="none" ;;
        bridge)      NETWORK_MODE="bridge" ;;
        *)           PROJECT_DIR="$arg" ;;
    esac
done

# --- Validate project directory ---
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Error: Directory '$PROJECT_DIR' does not exist."
    exit 1
fi

PROJECT_DIR=$(cd "$PROJECT_DIR" && pwd)
PROJECT_NAME=$(basename "$PROJECT_DIR")

# --- Read the GitHub token ---
# Instead of passing the token as an environment variable (where Claude can read it
# via `echo $GITHUB_TOKEN`, `env`, or `cat /proc/self/environ`), we write it to a
# root-owned credential file inside the container. The git credential helper reads
# from this file, but the `node` user cannot access it directly. This prevents
# Claude from extracting the raw token and using it with curl/wget to bypass the
# Git Guardian via the GitHub API.
if [ ! -f "$GITHUB_TOKEN_FILE" ]; then
    echo "Error: Store your GitHub PAT in $GITHUB_TOKEN_FILE"
    echo "  echo 'github_pat_xxxx' > $GITHUB_TOKEN_FILE && chmod 600 $GITHUB_TOKEN_FILE"
    exit 1
fi

GITHUB_TOKEN=$(cat "$GITHUB_TOKEN_FILE")

# Warn if token file has loose permissions
TOKEN_PERMS=$(stat -c '%a' "$GITHUB_TOKEN_FILE" 2>/dev/null || stat -f '%Lp' "$GITHUB_TOKEN_FILE" 2>/dev/null)
if [ -n "$TOKEN_PERMS" ] && [ "$TOKEN_PERMS" != "600" ] && [ "$TOKEN_PERMS" != "400" ]; then
    echo "WARNING: $GITHUB_TOKEN_FILE has permissions $TOKEN_PERMS (should be 600)"
    echo "  Fix: chmod 600 $GITHUB_TOKEN_FILE"
fi

# Claude Code authentication: handled by the volume mount -v "$HOME/.claude:/home/node/.claude"
# which makes ~/.claude/.credentials.json visible inside the container. Claude Code reads
# this file natively — no environment variable needed. Do NOT set CLAUDE_CODE_OAUTH_TOKEN:
# it overrides .credentials.json and breaks auth if the value is stale or malformed.

# --- Temp env file for non-sensitive vars only ---
ENV_FILE=$(mktemp)
chmod 600 "$ENV_FILE"
{api_key_block}
trap 'rm -f "$ENV_FILE"' EXIT

# --- Token expiry reminder ---
EXPIRY_FILE="$HOME/.alcatraz-token-expiry"
if [ -f "$EXPIRY_FILE" ]; then
    EXPIRY_DATE=$(cat "$EXPIRY_FILE")
    # macOS uses -j -f, Linux/WSL uses -d
    if date -j -f "%Y-%m-%d" "$EXPIRY_DATE" +%s > /dev/null 2>&1; then
        EXPIRY_TS=$(date -j -f "%Y-%m-%d" "$EXPIRY_DATE" +%s)
    else
        EXPIRY_TS=$(date -d "$EXPIRY_DATE" +%s 2>/dev/null || echo 0)
    fi
    NOW_TS=$(date +%s)
    if [ "$EXPIRY_TS" -gt 0 ]; then
        DAYS_LEFT=$(( (EXPIRY_TS - NOW_TS) / 86400 ))
        if [ "$DAYS_LEFT" -le 0 ]; then
            echo "GitHub PAT expired on $EXPIRY_DATE! Generate a new one:"
            echo "  1. Create a new token on GitHub"
            echo "  2. echo 'github_pat_xxxx' > ~/.alcatraz-token"
            echo "  3. echo 'YYYY-MM-DD' > ~/.alcatraz-token-expiry"
            exit 1
        elif [ "$DAYS_LEFT" -le 7 ]; then
            echo "WARNING: GitHub PAT expires in $DAYS_LEFT day(s) ($EXPIRY_DATE) -- rotate soon"
        fi
    fi
fi

# --- Read host git identity (fallback for inside the container) ---
HOST_GIT_NAME=$(git config user.name 2>/dev/null || echo "")
HOST_GIT_EMAIL=$(git config user.email 2>/dev/null || echo "")

# --- Ensure .claude.json exists as a file (not a directory) ---
if [ ! -f "$HOME/.claude.json" ]; then
    # Remove if Docker previously created it as a directory
    rm -rf "$HOME/.claude.json" 2>/dev/null
    echo '{{"hasCompletedOnboarding":true}}' > "$HOME/.claude.json"
fi

# --- Build port mapping arguments ---
PORT_ARGS=()
PORT_ENV_ARGS=()
CONTAINER_NAME="alcatraz-${{PROJECT_NAME}}-$$"

case "$PORT_MODE" in
    noports)
        # No port forwarding at all
        ;;
    fixed)
        # 1:1 mapping — only works for a single container
        for cport in "${{CONTAINER_PORTS[@]}}"; do
            PORT_ARGS+=(-p "127.0.0.1:${{cport}}:${{cport}}")
            PORT_ENV_ARGS+=(-e "HOST_PORT_${{cport}}=${{cport}}")
        done
        ;;
    *)
        # Deterministic: hash project name → stable base port in range 10000-59996
        # Multiply by 4 so each project gets a non-overlapping block of consecutive ports
        HASH=$(echo -n "$PROJECT_NAME" | cksum | awk '{{print $1}}')
        BASE_PORT=$(( (HASH % 12500) * ${{#CONTAINER_PORTS[@]}} + 10000 ))

        # Collision check: if base port is in use, offset by PID to find a free block
        if command -v ss &>/dev/null; then
            CHECK_CMD="ss -tlnH"
        elif command -v netstat &>/dev/null; then
            CHECK_CMD="netstat -tln"
        else
            CHECK_CMD=""
        fi

        if [ -n "$CHECK_CMD" ] && $CHECK_CMD 2>/dev/null | grep -q ":${{BASE_PORT}} "; then
            BASE_PORT=$(( BASE_PORT + ($$ % 5000) * ${{#CONTAINER_PORTS[@]}} ))
            # Wrap around if we exceed the safe range
            if [ "$BASE_PORT" -gt 59996 ]; then
                BASE_PORT=$(( (BASE_PORT % 50000) + 10000 ))
            fi
        fi

        OFFSET=0
        for cport in "${{CONTAINER_PORTS[@]}}"; do
            HPORT=$((BASE_PORT + OFFSET))
            PORT_ARGS+=(-p "127.0.0.1:${{HPORT}}:${{cport}}")
            PORT_ENV_ARGS+=(-e "HOST_PORT_${{cport}}=${{HPORT}}")
            OFFSET=$((OFFSET + 1))
        done
        ;;
esac

echo -e "\\033[1;36m════════════════════════════════════════════════════════════════════\\033[0m"
echo -e "\\033[1;36m █████╗ ██╗      ██████╗ █████╗ ████████╗██████╗  █████╗ ███████╗\\033[0m"
echo -e "\\033[1;36m██╔══██╗██║     ██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗╚══███╔╝\\033[0m"
echo -e "\\033[1;36m███████║██║     ██║     ███████║   ██║   ██████╔╝███████║  ███╔╝ \\033[0m"
echo -e "\\033[1;36m██╔══██║██║     ██║     ██╔══██║   ██║   ██╔══██╗██╔══██║ ███╔╝  \\033[0m"
echo -e "\\033[1;36m██║  ██║███████╗╚██████╗██║  ██║   ██║   ██║  ██║██║  ██║███████╗\\033[0m"
echo -e "\\033[1;36m╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝\\033[0m"
echo ""
echo "Project:  $PROJECT_NAME"
echo "Path:     $PROJECT_DIR"
echo "Network:  $NETWORK_MODE"

if [ "$PORT_MODE" = "noports" ]; then
    echo "Ports:    None (no port forwarding)"
elif [ "$PORT_MODE" = "fixed" ]; then
    echo "Ports:    ${{CONTAINER_PORTS[*]}} → localhost (1:1 fixed)"
else
    echo "Ports:"
    OFFSET=0
    for cport in "${{CONTAINER_PORTS[@]}}"; do
        HPORT=$((BASE_PORT + OFFSET))
        echo "          ${{cport}} → localhost:${{HPORT}}"
        OFFSET=$((OFFSET + 1))
    done
fi

echo -e "\\033[1;36m════════════════════════════════════════════════════════════════════\\033[0m"

# --- Optional volume mounts ---
# Uncomment and add these to the docker run command below as needed:
#   -v /var/run/docker.sock:/var/run/docker.sock       # Docker-in-Docker
#       ⚠ WARNING: Mounting the Docker socket grants FULL HOST ROOT ACCESS.
#       Any process in the container can create privileged containers, read
#       host files, and escape the sandbox entirely. Only enable this if you
#       understand the security implications.
#   -v "$HOME/.aws:/home/node/.aws:ro"                 # AWS credentials
#   -v "$HOME/.config/gcloud:/home/node/.config/gcloud:ro"  # GCP credentials
#   -v "$HOME/.azure:/home/node/.azure:ro"             # Azure credentials
#   -v "$HOME/.kube:/home/node/.kube:ro"               # Kubernetes config
#   -v "$HOME/.ssh:/home/node/.ssh:ro"                 # SSH keys

docker run -it --rm \\
{docker_flags_str} \\
    bash -c "
        # Ensure onboarding is marked complete (prevents setup wizard on every launch)
        if [ ! -f /home/node/.claude.json ] || ! grep -q \\"hasCompletedOnboarding\\" /home/node/.claude.json 2>/dev/null; then
            echo '{{\\"hasCompletedOnboarding\\":true}}' > /home/node/.claude.json
        fi

        # Write the GitHub token to a root-owned file that only root can read.
        # The git credential helper reads from this file, but the node user (Claude)
        # cannot access it directly — preventing token extraction via env, printenv,
        # or /proc/self/environ.
        # Uses here-string (<<<) instead of echo to avoid exposing the token in ps output.
        sudo tee /root/.git-credentials > /dev/null <<< '${{GITHUB_TOKEN}}'
        sudo chmod 600 /root/.git-credentials

        # Configure git to use a credential helper that reads the root-owned token.
        # The helper runs via sudo so it can read /root/.git-credentials, but the
        # token value never appears in the node user's environment or shell history.
        /usr/bin/git config --global credential.helper '!f() {{ echo \\"username=x-access-token\\"; echo \\"password=\\$(sudo cat /root/.git-credentials)\\"; }}; f'

        # Use the host git identity, falling back to the last commit author, then a safe default.
        GIT_NAME=\\"\\${{HOST_GIT_NAME:-\\$(/usr/bin/git -C /workspace log -1 --format=%an 2>/dev/null || echo Claude)}}\\"
        GIT_EMAIL=\\"\\${{HOST_GIT_EMAIL:-\\$(/usr/bin/git -C /workspace log -1 --format=%ae 2>/dev/null || echo claude@local)}}\\"
        /usr/bin/git config --global user.name \\"\\$GIT_NAME\\"
        /usr/bin/git config --global user.email \\"\\$GIT_EMAIL\\"
{chrome_block}
        # Build port mapping context for Claude so it tells users the correct host URLs
        PORT_CONTEXT=''
        for var in \\$(env | grep ^HOST_PORT_ | sort); do
            CPORT=\\$(echo \\"\\$var\\" | sed 's/HOST_PORT_//;s/=.*//')
            HPORT=\\$(echo \\"\\$var\\" | sed 's/.*=//')
            PORT_CONTEXT=\\"\\${{PORT_CONTEXT}}  container port \\$CPORT → host localhost:\\$HPORT
\\"
        done
        if [ -n \\"\\$PORT_CONTEXT\\" ]; then
            PORT_PROMPT=\\"You are running inside a Docker container. Container ports are mapped to the host as follows:
\\${{PORT_CONTEXT}}
When the user starts a dev server or asks to view a URL, ALWAYS tell them the HOST port from the mappings above (e.g., for port 3000 use localhost:\\$(env | grep ^HOST_PORT_3000= | cut -d= -f2)), NEVER the container-internal port.\\"
        else
            PORT_PROMPT=\\"You are running inside a Docker container with no port forwarding. Dev servers started here won't be accessible from the host browser.\\"
        fi

        # --dangerously-skip-permissions is intentional here: the Docker container
        # itself is the sandbox, and git-guardian.sh handles git-specific safety.
        {timeout_prefix}claude --dangerously-skip-permissions --append-system-prompt \\"\\$PORT_PROMPT\\"
    "
''')


def generate_pretool_hook(config: SetupConfig) -> str:
    branches_re = '|'.join(config.protected_branches)
    return textwrap.dedent(f'''\
#!/bin/bash
# pretool-hook.sh — PreToolUse hook that blocks dangerous commands
# Generated by Alcatraz Setup Wizard v{VERSION}
# Exit code 0 = allow, exit code 2 = block

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL_NAME" != "Bash" ] || [ -z "$COMMAND" ]; then
    exit 0
fi

BLOCKED_PATTERNS=(
    'git push.*--force'
    'git push.*-f\\b'
    'git push.*--delete'
    'git push.*:refs/'
    'git stash clear'
    # Block deletion of protected branches (main, master, etc.)
    'git branch.*(-[dD]|--delete).*\\b({branches_re})\\b'
    'gh repo delete'
    'gh api.*/repos.*DELETE'
    'curl.*api\\.github\\.com'
    'wget.*api\\.github\\.com'
    'rm -rf /'
    'rm -rf ~'
    'rm -rf /home'
    'rm -rf /workspace'
    'chmod 777'
    # Block ANY direct reference to .git-credentials — catches sudo cat,
    # sudo /bin/cat, sudo /usr/bin/cat, head, tee overwrite, etc.
    # Safe because the credential helper runs inside git (not via Bash tool).
    '\\.git-credentials'
    # Prevent bypassing Git Guardian via sudo or direct binary path
    'sudo.*/usr/bin/git'
    'sudo.*git push'
    # Prevent reconfiguring the credential helper to extract the token
    'git config.*credential\\.helper'
    # Prevent command obfuscation to bypass pattern checks
    'base64.*-[dD].*\\|.*bash'
    'base64.*--decode.*\\|.*sh'
    'base64.*-[dD].*\\|.*eval'
    # Prevent env inspection for token extraction
    '/proc/.*environ'
)

for pattern in "${{BLOCKED_PATTERNS[@]}}"; do
    if echo "$COMMAND" | grep -qE "$pattern"; then
        echo "BLOCKED by PreToolUse hook: matches '$pattern'" >&2
        echo "Command was: $COMMAND" >&2
        exit 2
    fi
done

exit 0
''')


def generate_settings_json(config: SetupConfig) -> str:
    settings = {}

    # MCP servers — Playwright for browser automation and testing
    if config.include_browser:
        settings["mcpServers"] = {
            "playwright": {
                "command": "npx",
                "args": [
                    "-y",
                    "@anthropic-ai/mcp-server-playwright",
                ],
            }
        }

    if config.enable_deny_list:
        deny = [
            "Bash(gh repo delete*)",
            "Bash(gh api */repos*DELETE*)",
            "Bash(curl*api.github.com*)",
            "Bash(wget*api.github.com*)",
            "Bash(rm -rf /*)",
            "Bash(rm -rf ~*)",
            "Bash(chmod 777*)",
            "Bash(git push*--force*)",
            "Bash(git push*--delete*)",
            "Bash(git stash clear*)",
        ]
        # Block deletion of protected branches
        for branch in config.protected_branches:
            deny.append(f"Bash(git branch*-d *{branch}*)")
            deny.append(f"Bash(git branch*-D *{branch}*)")
            deny.append(f"Bash(git branch*--delete *{branch}*)")
        settings["permissions"] = {"deny": deny}

    if config.enable_pretool_hook:
        settings["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/workspace/.claude/hooks/pretool-hook.sh"
                        }
                    ]
                }
            ]
        }

    return json.dumps(settings, indent=2)


def generate_branch_ruleset() -> str:
    """Return the recommended GitHub branch ruleset JSON for import.

    This ruleset protects the default branch with the same rules shown in the
    Branch Protection wizard step: no deletions, no force-pushes, and pull
    requests required with zero approvals (solo-friendly default).  Users
    import this file directly via Settings → Rules → Rulesets → New →
    Import a ruleset.  Teams should increase required approvals to 1+.
    """
    ruleset = {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "exclude": [],
                "include": ["~DEFAULT_BRANCH"],
            }
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "required_reviewers": [],
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
        ],
        "bypass_actors": [],
    }
    return json.dumps(ruleset, indent=2)


def generate_wrapper_script(config: SetupConfig) -> str:
    """Generate the `alcatraz` launcher wrapper that can be placed on PATH."""
    return textwrap.dedent(f'''\
#!/bin/bash
# alcatraz — Quick launcher for Alcatraz Docker environment
# Generated by Alcatraz Setup Wizard v{VERSION}
#
# Usage:
#   alcatraz                          # Current dir, default settings
#   alcatraz /path/to/project         # Specific project
#   alcatraz . fixed                  # Fixed ports
#   alcatraz none                     # Offline mode
#   alcatraz /path/to/project none noports
#
# All arguments are forwarded directly to run.sh.
# Install to PATH:  ln -sf {config.install_dir}/alcatraz ~/.local/bin/alcatraz

INSTALL_DIR="{config.install_dir}"
RUN_SCRIPT="$INSTALL_DIR/run.sh"

if [ ! -f "$RUN_SCRIPT" ]; then
    echo "Error: Alcatraz not found at $INSTALL_DIR"
    echo ""
    echo "Either the installation was moved or not yet completed."
    echo "Re-run the setup wizard or update the INSTALL_DIR in this script."
    exit 1
fi

exec "$RUN_SCRIPT" "$@"
''')


# ══════════════════════════════════════════════════════════════════
#  STEP 6 — REVIEW & GENERATE
# ══════════════════════════════════════════════════════════════════

def step_review_and_generate(config: SetupConfig, came_from="next"):
    show_step_header(7, TOTAL_STEPS, "Review & Generate",
                     "Confirm your choices and generate all configuration files")

    # ── Validation gate — check all steps are complete ──
    missing = config.incomplete_steps()
    if missing:
        console.print("  [bold red]⚠  Cannot generate — the following steps are incomplete:[/]\n")
        for idx in missing:
            console.print(f"    [red]○[/] Step {idx + 2}: {config.STEP_NAMES[idx]}")
        console.print()
        console.print("  [dim]Go back and complete the incomplete steps, then return here.[/]")
        console.print()

        action = questionary.select(
            "",
            choices=[
                questionary.Choice("← Go back to complete steps", value="__back__"),
            ],
            style=q_style,
        ).ask()
        if action is None:
            sys.exit(0)
        return "back"

    # Summary table
    summary_w = min(console.width - 6, 100)
    table = Table(title="Configuration Summary", box=box.ROUNDED,
                  border_style="cyan", show_header=True, header_style="bold white",
                  padding=(0, 2), title_style="bold cyan", width=summary_w)
    table.add_column("Setting", style="bold", no_wrap=True)
    table.add_column("Value")

    table.add_row("Install directory", config.install_dir)

    # Profile + components
    components = []
    if config.include_github_cli: components.append("GitHub CLI")
    if config.include_docker_cli: components.append("Docker CLI")
    if config.include_cloud_clis: components.append("Cloud CLIs")
    if config.include_infra_tools: components.append("Terraform/kubectl")
    if config.include_db_clients: components.append("DB clients")
    if config.include_browser: components.append("Chrome/Playwright")
    if config.include_ml_packages: components.append("ML packages")
    comp_str = ", ".join(components) if components else "Core only"
    table.add_row("Profile", f"{config.profile.title()}  ({comp_str})")

    table.add_row("Protected branches", ", ".join(config.protected_branches))
    table.add_row("Default network", config.default_network)

    port_mode_labels = {
        "deterministic": "Deterministic (parallel safe)",
        "fixed": "Fixed 1:1 (single container)",
        "noports": "None (no forwarding)",
    }
    table.add_row("Port mode", port_mode_labels.get(config.port_mode, config.port_mode))
    if config.port_mode != "noports":
        all_ports = config.ports + [int(p) for p in config.custom_ports if p.isdigit()]
        table.add_row("Container ports", ", ".join(str(p) for p in all_ports))

    security_layers = []
    if config.enable_deny_list: security_layers.append("Deny list")
    if config.enable_pretool_hook: security_layers.append("PreToolUse hook")
    if config.enable_session_timeout: security_layers.append(f"Timeout ({config.session_timeout_hours}h)")
    if config.enable_readonly_hooks: security_layers.append("Read-only hooks")
    if config.enable_resource_limits: security_layers.append(f"Resources ({config.resource_memory}, {config.resource_cpus} CPUs)")
    table.add_row("Security layers", ", ".join(security_layers) if security_layers else "None")

    table.add_row("PAT type", config.pat_type.title())
    table.add_row("Auth method", "OAuth" if config.auth_method == "oauth" else "API Key")

    console.print(Padding(table, (0, 2)))
    console.print()

    # Files to generate
    console.print("  [bold]Files that will be created:[/]")
    console.print(f"  [cyan]  {config.install_dir}/[/]")
    console.print(f"  [cyan]  ├── Dockerfile[/]")
    console.print(f"  [cyan]  ├── git-guardian.sh[/]")
    console.print(f"  [cyan]  ├── run.sh[/]")
    console.print(f"  [cyan]  ├── alcatraz[/]              [dim](launcher — add to PATH)[/]")
    if config.enable_pretool_hook:
        console.print(f"  [cyan]  ├── pretool-hook.sh[/]")
    if config.enable_deny_list or config.enable_pretool_hook:
        console.print(f"  [cyan]  ├── settings.json[/]       [dim](copy to project .claude/)[/]")
    console.print(f"  [cyan]  ├── branch-ruleset.json[/] [dim](import into GitHub rulesets)[/]")
    console.print(f"  [cyan]  ├── build.sh[/]")
    console.print(f"  [cyan]  └── auth.sh[/]              [dim](one-time OAuth login)[/]")
    console.print()

    action = questionary.select(
        "Ready to generate?",
        choices=[
            questionary.Choice("✓ Generate files with these settings", value="generate"),
            questionary.Choice("← Go back to change settings", value="__back__"),
        ],
        style=q_style,
    ).ask()

    if action is None:
        sys.exit(0)
    if action == "__back__":
        return "back"

    # ── Generate! ──
    install_dir = Path(config.install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    def make_executable(path: Path):
        """chmod +x, ignoring errors on filesystems that don't support it (e.g. NTFS via WSL)."""
        try:
            path.chmod(0o755)
        except OSError:
            pass

    def write_lf(path: Path, content: str):
        """Write file with Unix (LF) line endings — required for bash scripts on WSL."""
        path.write_text(content, newline="\n")

    # Calculate actual file count for accurate progress
    file_count = 7  # Dockerfile, git-guardian.sh, run.sh, alcatraz, build.sh, branch-ruleset.json, auth.sh
    if config.enable_pretool_hook:
        file_count += 1
    if config.enable_deny_list or config.enable_pretool_hook:
        file_count += 1

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        ) as progress:
            task = progress.add_task("Generating files...", total=file_count)

            # Dockerfile
            write_lf(install_dir / "Dockerfile", generate_dockerfile(config))
            progress.update(task, advance=1, description="Wrote Dockerfile")

            # Git Guardian
            guardian_path = install_dir / "git-guardian.sh"
            write_lf(guardian_path, generate_git_guardian(config))
            make_executable(guardian_path)
            progress.update(task, advance=1, description="Wrote git-guardian.sh")

            # run.sh
            run_path = install_dir / "run.sh"
            write_lf(run_path, generate_run_script(config))
            make_executable(run_path)
            progress.update(task, advance=1, description="Wrote run.sh")

            # alcatraz launcher wrapper
            wrapper_path = install_dir / "alcatraz"
            write_lf(wrapper_path, generate_wrapper_script(config))
            make_executable(wrapper_path)
            progress.update(task, advance=1, description="Wrote alcatraz")

            # PreToolUse hook
            if config.enable_pretool_hook:
                hook_path = install_dir / "pretool-hook.sh"
                write_lf(hook_path, generate_pretool_hook(config))
                make_executable(hook_path)
                progress.update(task, advance=1, description="Wrote pretool-hook.sh")

            # settings.json
            if config.enable_deny_list or config.enable_pretool_hook:
                write_lf(install_dir / "settings.json", generate_settings_json(config))
                progress.update(task, advance=1, description="Wrote settings.json")

            # branch-ruleset.json
            write_lf(install_dir / "branch-ruleset.json", generate_branch_ruleset())
            progress.update(task, advance=1, description="Wrote branch-ruleset.json")

            # build.sh
            build_script = textwrap.dedent(f"""\
#!/bin/bash
set -e
# build.sh — Build the Alcatraz Docker image
# Generated by Alcatraz Setup Wizard v{VERSION}

echo "Building Alcatraz Docker image..."
echo "This may take 10-15 minutes on first build."
echo ""

cd "$(dirname "$0")"

docker build -t alcatraz:latest \\
  --build-arg USER_UID=$(id -u) \\
  --build-arg USER_GID=$(id -g) .

echo ""
echo "Image built successfully!"
echo ""
echo "Launch on a project:"
echo "  alcatraz /path/to/project"
""")
            build_path = install_dir / "build.sh"
            write_lf(build_path, build_script)
            make_executable(build_path)
            progress.update(task, advance=1, description="Wrote build.sh")

            # auth.sh
            auth_script = textwrap.dedent(f"""\
#!/bin/bash
set -e
# auth.sh — One-time OAuth login for Claude Code
# Generated by Alcatraz Setup Wizard v{VERSION}

echo "Setting up Claude Code authentication..."

# Ensure credential paths exist on host
if [ -d "$HOME/.claude.json" ]; then
  echo "Warning: $HOME/.claude.json is a directory — removing it"
  rm -rf "$HOME/.claude.json"
fi
if [ ! -f "$HOME/.claude.json" ]; then
  echo '{{"hasCompletedOnboarding":true}}' > "$HOME/.claude.json"
fi
mkdir -p "$HOME/.claude"

echo ""
echo "Launching auth container — a browser window will open."
echo "Complete the login, then type /exit in the terminal."
echo ""

docker run -it --rm \\
  -v "$HOME/.claude:/home/node/.claude" \\
  -v "$HOME/.claude.json:/home/node/.claude.json" \\
  alcatraz:latest \\
  claude --dangerously-skip-permissions
""")
            auth_path = install_dir / "auth.sh"
            write_lf(auth_path, auth_script)
            make_executable(auth_path)
            progress.update(task, advance=1, description="Wrote auth.sh")

    except Exception as e:
        console.print()
        console.print(f"  [red bold]✗ Error generating files:[/] [red]{e}[/]")
        console.print(f"  [dim]Directory: {install_dir}[/]")
        console.print()
        import traceback
        console.print(f"  [dim]{traceback.format_exc()}[/]")
        sys.exit(1)

    # ── Brief success ──
    console.print()
    console.print(f"  [bold green]✓[/] All files generated in [cyan]{config.install_dir}[/]")
    console.print()
    pause()
    return "next"


# ══════════════════════════════════════════════════════════════════
#  STEP 7 — GITHUB PAT CREATION
# ══════════════════════════════════════════════════════════════════

def step_github_pat_creation(config: SetupConfig, came_from="next"):
    """Guide the user through creating a GitHub Personal Access Token."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(8, TOTAL_STEPS, "Create GitHub Personal Access Token",
                         "Create a scoped token for secure Docker container access")

        if config.pat_type == "fine-grained":
            show_info_box("Fine-Grained PAT — Step by Step", textwrap.dedent("""
                [bold]1.[/] Go to [cyan]GitHub → Settings → Developer Settings → Fine-grained tokens[/]
                [bold]2.[/] Click [bold]Generate new token[/]
                [bold]3.[/] Configure:
                   [bold]Token name:[/]         alcatraz
                   [bold]Expiration:[/]         30–90 days (rotate regularly)
                   [bold]Resource owner:[/]     Your team org
                   [bold]Repository access:[/]  [cyan]Only select repositories[/] — pick repos you'll use
            """).strip())

            console.print()

            # Permissions table
            half_w = max((console.width - 8) // 2, 40)
            perm_table = Table(title="Permissions to Grant", box=box.ROUNDED,
                               border_style="green", width=half_w)
            perm_table.add_column("Permission", style="bold")
            perm_table.add_column("Level", style="cyan")
            perm_table.add_column("Required?")
            perm_table.add_row("Contents", "Read & Write", "Yes — push/pull code")
            perm_table.add_row("Metadata", "Read-only", "Yes — required by default")
            perm_table.add_row("Pull requests", "Read & Write", "Optional — open PRs")
            perm_table.add_row("Commit statuses", "Read & Write", "Optional — CI status checks")
            perm_table.add_row("Issues", "Read & Write", "Optional — create/close issues")
            perm_table.add_row("Actions", "Read-only", "Optional — check CI workflows")

            # Never grant
            deny_table = Table(title="Never Grant These", box=box.ROUNDED,
                               border_style="red", width=half_w)
            deny_table.add_column("Permission", style="bold red")
            deny_table.add_column("Risk")
            deny_table.add_row("Administration", "Allows repo deletion and settings changes")
            deny_table.add_row("Secrets", "Exposes CI/CD API keys and credentials")
            deny_table.add_row("Workflows (write)", "Can modify CI pipelines to exfiltrate secrets")
            deny_table.add_row("Actions (write)", "Can trigger/cancel deployment pipelines")
            deny_table.add_row("Webhooks", "Data exfiltration via arbitrary URLs")
            deny_table.add_row("Environments", "Controls deployment environment protection")
            deny_table.add_row("Deployments", "Can bypass CI and deploy directly")

            # Side-by-side layout
            console.print(Columns([perm_table, deny_table], padding=(0, 2)))
            console.print()

            show_info_box("Important Warning", textwrap.dedent("""
                [yellow bold]Do NOT edit an existing fine-grained PAT on GitHub.[/]

                There is a known GitHub bug where editing silently reverts
                "Only select repositories" back to "All repositories".
                Instead, [bold]delete and recreate[/] the token when you need changes.
            """).strip(), style="yellow")
        else:
            # Classic PAT instructions
            show_info_box("Classic PAT — Step by Step", textwrap.dedent("""
                [bold]1.[/] Go to [cyan]GitHub → Settings → Developer Settings → PATs (classic)[/]
                [bold]2.[/] Click [bold]Generate new token[/]
                [bold]3.[/] Configure:
                   [bold]Note:[/]        alcatraz
                   [bold]Expiration:[/]  30–90 days
                   [bold]Scopes:[/]      Only select [cyan]repo[/] (full control of private repos)

                [bold]4.[/] Do [bold red]NOT[/] select: delete_repo, admin:org, admin:repo_hook, gist
                [bold]5.[/] If your org uses SAML SSO, click [bold]"Configure SSO" → "Authorize"[/]
            """).strip())

            console.print()
            show_info_box("Classic PAT Limitation", textwrap.dedent("""
                [yellow]The repo scope grants access to ALL repos you can access,
                not just selected ones. This makes branch protection and
                Git Guardian even more important as compensating controls.[/]
            """).strip(), style="yellow")

        console.print()
        console.print("  [bold]Copy the token before leaving the page — you won't see it again.[/]")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([], initial_nav=nav)

        if result == "back":
            return "back"
        config.mark_complete(5)
        return "next"


# ══════════════════════════════════════════════════════════════════
#  STEP 8 — TOKEN STORAGE
# ══════════════════════════════════════════════════════════════════

def _store_token(config: SetupConfig):
    """Collect and store the GitHub PAT and expiry date."""
    home = os.path.expanduser("~")
    token_path = os.path.join(home, ".alcatraz-token")
    expiry_path = os.path.join(home, ".alcatraz-token-expiry")

    # Token input (masked)
    token = questionary.password(
        "Paste your GitHub PAT:",
        style=q_style,
    ).ask()
    if token is None:
        sys.exit(0)
    token = token.strip()
    if not token:
        console.print("  [yellow]No token entered — skipping.[/]")
        return

    # Basic format check
    if not (token.startswith("ghp_") or token.startswith("github_pat_")):
        console.print("  [yellow]Warning: token doesn't start with ghp_ or github_pat_[/]")
        proceed = questionary.confirm(
            "Store it anyway?",
            default=True,
            style=q_style,
        ).ask()
        if not proceed:
            return

    # Expiry date
    expiry = questionary.text(
        "Token expiry date (YYYY-MM-DD):",
        style=q_style,
    ).ask()
    if expiry is None:
        sys.exit(0)
    expiry = expiry.strip()
    if expiry and not re.match(r'^\d{4}-\d{2}-\d{2}$', expiry):
        console.print("  [yellow]Invalid date format — skipping expiry file.[/]")
        expiry = ""

    # Write token
    try:
        with open(token_path, "w") as f:
            f.write(token + "\n")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass  # NTFS via WSL may not support chmod
        console.print(f"  [green]✓[/] Token saved to [cyan]{token_path}[/]")

        if expiry:
            with open(expiry_path, "w") as f:
                f.write(expiry + "\n")
            console.print(f"  [green]✓[/] Expiry saved to [cyan]{expiry_path}[/]")

        config.mark_complete(6)
    except Exception as e:
        console.print(f"  [red]✗ Error writing token: {e}[/]")


def step_token_storage(config: SetupConfig, came_from="next"):
    """Collect and securely store the GitHub PAT."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(9, TOTAL_STEPS, "Store GitHub Token",
                         "Save your PAT securely for Docker container access")

        home = os.path.expanduser("~")
        token_path = os.path.join(home, ".alcatraz-token")
        expiry_path = os.path.join(home, ".alcatraz-token-expiry")

        # Show current state
        token_exists = os.path.isfile(token_path)
        if token_exists:
            expiry_str = ""
            if os.path.isfile(expiry_path):
                try:
                    expiry_str = open(expiry_path).read().strip()
                except Exception:
                    pass
            console.print(f"  [green]✓[/] Token file exists: [cyan]{token_path}[/]")
            if expiry_str:
                console.print(f"    Expiry: [cyan]{expiry_str}[/]")
            console.print()
        else:
            console.print(f"  [dim]○[/] No token file found at [cyan]{token_path}[/]")
            console.print()

        show_info_box("What Happens", textwrap.dedent("""
            Your token is stored in [cyan]~/.alcatraz-token[/] with restricted
            permissions (chmod 600). The launch script reads it at runtime and
            injects it into the container as a root-owned credential — Claude
            cannot access the raw token value.
        """).strip())
        console.print()

        choices = [("Store token now", "store")]
        if token_exists:
            choices = [("Replace existing token", "store")]

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu(choices, initial_nav=nav)

        if result == "back":
            return "back"
        if result == "next":
            if not token_exists:
                console.print()
                console.print("  [bold red]⚠  Cannot continue — GitHub token not stored[/]")
                console.print(f"    Required: [cyan]{token_path}[/]")
                console.print()
                console.print("  [dim]Use 'Store token now' to save your PAT first.[/]")
                pause()
                continue
            config.mark_complete(6)
            return "next"
        # result == "store"
        _store_token(config)


# ══════════════════════════════════════════════════════════════════
#  STEP 9 — DOCKER BUILD
# ══════════════════════════════════════════════════════════════════

def step_docker_build(config: SetupConfig, came_from="next"):
    """Build the Docker image from the generated Dockerfile."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(10, TOTAL_STEPS, "Build Docker Image",
                         "Build the Alcatraz Docker image")

        # Estimate build time based on profile
        estimates = {
            "minimal": ("5–8 minutes", "~1.5–2 GB"),
            "recommended": ("10–15 minutes", "~4–5 GB"),
            "full": ("15–20 minutes", "~7–8 GB"),
            "custom": ("10–20 minutes", "varies"),
        }
        time_est, size_est = estimates.get(config.profile, ("10–15 minutes", "varies"))

        console.print(f"  [bold]Profile:[/]         {config.profile.title()}")
        console.print(f"  [bold]Estimated time:[/]  {time_est}")
        console.print(f"  [bold]Image size:[/]      {size_est}")
        console.print(f"  [bold]Build dir:[/]       {config.install_dir}")
        console.print()

        # Check if image already exists
        try:
            check = subprocess.run(
                ["docker", "images", "-q", "alcatraz:latest"],
                capture_output=True, text=True, timeout=10
            )
            if check.stdout.strip():
                console.print("  [green]✓[/] Image [cyan]alcatraz:latest[/] already exists")
                console.print("    [dim]Building again will replace it.[/]")
                console.print()
        except Exception:
            pass

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([
            ("Build now", "build"),
        ], initial_nav=nav)

        if result == "back":
            return "back"
        if result == "next":
            try:
                check = subprocess.run(
                    ["docker", "images", "-q", "alcatraz:latest"],
                    capture_output=True, text=True, timeout=10
                )
                image_exists = bool(check.stdout.strip())
            except Exception:
                image_exists = False
            if not image_exists:
                console.print()
                console.print("  [bold red]⚠  Cannot continue — Docker image not built[/]")
                console.print("    Required: [cyan]alcatraz:latest[/]")
                console.print()
                console.print("  [dim]Use 'Build now' to create the Docker image first.[/]")
                pause()
                continue
            return "next"

        # result == "build"
        clear_screen()
        show_banner()
        show_step_header(10, TOTAL_STEPS, "Build Docker Image",
                         "Building the Alcatraz Docker image")
        console.print()
        console.print("  [dim]Ctrl+C to cancel (you can run ./build.sh later).[/]")
        console.print()

        term_h = shutil.get_terminal_size().lines
        # Reserve more space for the step UI already on screen
        output_max = max(5, min(term_h - 15, 25))

        # Live renderable: __rich_console__ is called on every refresh cycle,
        # so the elapsed timer updates automatically (refresh_per_second=4).
        class _Display:
            def __init__(self):
                self.lines = []
                self.start = time.monotonic()
            def __rich_console__(self, _con, _opts):
                elapsed = int(time.monotonic() - self.start)
                m, s = divmod(elapsed, 60)
                visible = self.lines[-output_max:]
                out = "\n".join(visible) if visible else "  Starting build..."
                yield Panel(
                    Text.from_ansi(out),
                    title=f"[bold]Build Output[/] [dim]({len(self.lines)} lines, {m}:{s:02d} elapsed)[/]",
                    border_style="dim cyan",
                    height=output_max + 2,
                )

        display = _Display()

        try:
            build_env = os.environ.copy()
            build_env["BUILDKIT_PROGRESS"] = "plain"
            proc = subprocess.Popen(
                ["bash", os.path.join(config.install_dir, "build.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=config.install_dir,
                env=build_env,
            )

            # Use a background thread to read subprocess output so the
            # main thread never blocks on BuildKit's \r-based progress
            # lines that lack a trailing \n.
            output_q = queue.Queue()

            def _reader():
                for line in proc.stdout:
                    output_q.put(line)
                output_q.put(None)          # sentinel: EOF

            threading.Thread(target=_reader, daemon=True).start()

            with Live(display, console=console, refresh_per_second=4) as live:
                while True:
                    try:
                        line = output_q.get(timeout=0.1)
                        if line is None:
                            break
                        for part in line.split("\r"):
                            stripped = part.rstrip()
                            if stripped:
                                display.lines.append(stripped)
                    except queue.Empty:
                        pass                # no output yet — Live auto-refreshes
                proc.wait()

            if proc.returncode == 0:
                console.print()
                console.print("  [bold green]✓ Docker image built successfully![/]")
                config.mark_complete(7)
                console.print()
                pause()
                return "next"
            else:
                console.print()
                console.print(f"  [bold red]✗ Build failed (exit code {proc.returncode})[/]")
                console.print("  [dim]Check the output above for errors, or re-run ./build.sh[/]")
        except KeyboardInterrupt:
            console.print("\n  [yellow]Build cancelled. Run ./build.sh manually later.[/]")
        except Exception as e:
            console.print(f"\n  [red]✗ Build error: {e}[/]")
            console.print("  [dim]Make sure Docker is running, then try ./build.sh manually.[/]")

        console.print()
        pause()


# ══════════════════════════════════════════════════════════════════
#  STEP 10 — CLAUDE AUTHENTICATION
# ══════════════════════════════════════════════════════════════════

def _run_oauth_auth(config: SetupConfig):
    """Execute auth.sh interactively for OAuth login."""
    clear_screen()
    show_banner()
    show_step_header(11, TOTAL_STEPS, "Authenticate Claude Code",
                     "Running OAuth login…")
    console.print()
    console.print("  [dim]A browser window will open. Complete the login,\n"
                  "  then type [cyan]/exit[/cyan] in the terminal.[/dim]")
    console.print()

    auth_script = os.path.join(config.install_dir, "auth.sh")
    if not os.path.isfile(auth_script):
        console.print(f"  [bold red]✗ Auth script not found:[/] [cyan]{auth_script}[/]")
        console.print("  [dim]Go back to Step 7 (Review & Generate) to generate files first.[/]")
        console.print()
        pause()
        return

    try:
        proc = subprocess.run(
            ["bash", auth_script],
            cwd=config.install_dir,
        )
        if proc.returncode == 0:
            console.print()
            console.print("  [bold green]✓ Authentication completed![/]")
        else:
            console.print()
            console.print(f"  [yellow]Auth script exited with code {proc.returncode}[/]")
            console.print(f"  [dim]You can retry or run manually: {auth_script}[/]")
    except KeyboardInterrupt:
        console.print(f"\n  [yellow]Auth cancelled. You can retry or run manually: {auth_script}[/]")
    except Exception as e:
        console.print(f"\n  [red]✗ Auth error: {e}[/]")
        console.print(f"  [dim]You can run manually: {auth_script}[/]")

    console.print()
    pause()


def step_claude_auth(config: SetupConfig, came_from="next"):
    """Guide through Claude Code OAuth authentication."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(11, TOTAL_STEPS, "Authenticate Claude Code",
                         "One-time OAuth login to connect Claude Code to your account")

        # Check if already authenticated
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        api_key_path = os.path.expanduser("~/.alcatraz-anthropic-key")
        if config.auth_method == "oauth":
            already_auth = os.path.isfile(creds_path)
            check_path = creds_path
        else:
            already_auth = os.path.isfile(api_key_path)
            check_path = api_key_path

        if already_auth:
            console.print(f"  [green]✓[/] Already authenticated — credentials found at:")
            console.print(f"    [cyan]{check_path}[/]")
            console.print()
        else:
            console.print(f"  [dim]○[/] Not yet authenticated")
            console.print(f"    Expected: [cyan]{check_path}[/]")
            console.print()

        if config.auth_method == "oauth":
            show_info_box("OAuth Authentication", textwrap.dedent("""
                Claude Code authenticates via OAuth (browser-based login).

                This will open a browser window — complete the login,
                then type [cyan]/exit[/] in the terminal.
            """).strip())
        else:
            show_info_box("API Key Authentication", textwrap.dedent("""
                Store your Anthropic API key:
                  [cyan]echo 'sk-ant-xxxxx' > ~/.alcatraz-anthropic-key[/]

                The key will be injected into the container at launch.
            """).strip())

        console.print()

        choices = []
        if config.auth_method == "oauth":
            if already_auth:
                choices.append(("Re-run OAuth login", "auth"))
            else:
                choices.append(("Run OAuth login", "auth"))

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu(choices, initial_nav=nav)

        if result == "back":
            return "back"
        if result == "next":
            if not already_auth:
                console.print()
                console.print("  [bold red]⚠  Cannot continue — Claude Code not authenticated[/]")
                console.print(f"    Required: [cyan]{check_path}[/]")
                console.print()
                if config.auth_method == "oauth":
                    console.print("  [dim]Use 'Run OAuth login' above to authenticate first.[/]")
                else:
                    console.print("  [dim]Store your API key in [cyan]~/.alcatraz-anthropic-key[/dim] first.[/]")
                pause()
                continue
            config.mark_complete(8)
            return "next"

        # result == "auth"
        _run_oauth_auth(config)


# ══════════════════════════════════════════════════════════════════
#  STEP 11 — PROJECT SETTINGS (settings.json)
# ══════════════════════════════════════════════════════════════════

def _copy_project_settings(config: SetupConfig):
    """Copy settings.json and hooks to a project directory."""
    project_dir = questionary.text(
        "Project directory path (must be WSL path, e.g. /mnt/c/Users/you/project):",
        style=q_style,
    ).ask()
    if project_dir is None:
        sys.exit(0)
    project_dir = os.path.expanduser(project_dir.strip())

    if not os.path.isdir(project_dir):
        console.print(f"  [red]✗ Directory not found: {project_dir}[/]")
        return

    # Check if it's a git repo
    if not os.path.isdir(os.path.join(project_dir, ".git")):
        console.print(f"  [yellow]Warning: {project_dir} doesn't appear to be a git repo.[/]")
        proceed = questionary.confirm("Continue anyway?", default=False, style=q_style).ask()
        if not proceed:
            return

    claude_dir = os.path.join(project_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # Copy settings.json
    src_settings = os.path.join(config.install_dir, "settings.json")
    if os.path.isfile(src_settings):
        dst_settings = os.path.join(claude_dir, "settings.json")
        shutil.copy2(src_settings, dst_settings)
        console.print(f"  [green]✓[/] Copied settings.json → [cyan]{dst_settings}[/]")
    else:
        console.print("  [dim]No settings.json to copy (deny list/hooks not enabled).[/]")

    # Copy pretool hook
    if config.enable_pretool_hook:
        hooks_dir = os.path.join(claude_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        src_hook = os.path.join(config.install_dir, "pretool-hook.sh")
        if os.path.isfile(src_hook):
            dst_hook = os.path.join(hooks_dir, "pretool-hook.sh")
            shutil.copy2(src_hook, dst_hook)
            try:
                os.chmod(dst_hook, 0o755)
            except OSError:
                pass
            console.print(f"  [green]✓[/] Copied pretool-hook.sh → [cyan]{dst_hook}[/]")

    console.print()
    console.print("  [bold]Next:[/] Commit these files to git so your team gets them too:")
    console.print(f"    [cyan]cd {project_dir}[/]")
    console.print(f"    [cyan]git add .claude/[/]")
    console.print(f"    [cyan]git commit -m 'Add Claude Code safety settings'[/]")
    console.print()
    config.mark_complete(9)


def step_project_settings(config: SetupConfig, came_from="next"):
    """Explain and deploy .claude/settings.json to a project."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(12, TOTAL_STEPS, "Project Settings",
                         "Deploy safety rules to your project's .claude/ directory")

        show_info_box("What is .claude/settings.json?", textwrap.dedent("""
            Claude Code reads [cyan].claude/settings.json[/] from each project.
            It contains [bold]deny rules[/] that block dangerous commands
            [bold]before[/] they execute — even in --dangerously-skip-permissions mode.

            [bold]Evaluation order:[/] deny → ask → allow → permission mode
            A matching deny rule blocks the tool call regardless of mode.
        """).strip())

        console.print()

        # Security layers diagram + limitation side by side
        layer_table = Table(title="How Settings.json Fits the Security Stack",
                            box=box.ROUNDED, border_style="cyan")
        layer_table.add_column("Layer", style="bold")
        layer_table.add_column("Protects")
        layer_table.add_column("Level")
        layer_table.add_row("Docker container", "Host filesystem", "OS-level")
        layer_table.add_row("PAT scoping", "GitHub permissions", "Server-side")
        layer_table.add_row("Branch protection", "Protected branches", "Server-side")
        layer_table.add_row("Git Guardian", "Dangerous git commands", "Binary wrapper")
        layer_table.add_row("[bold cyan]settings.json[/]", "[bold cyan]Deny list + hooks[/]", "[bold cyan]Application[/]")
        layer_table.add_row("PreToolUse hook", "Command patterns", "Application")

        limitation_panel = Panel(
            textwrap.dedent("""
                [yellow]Deny rules use string matching on tool calls,
                not on what commands actually do.[/] Claude could
                rephrase a blocked command. That's why deny rules
                complement (not replace) the other layers.

                The settings.json should be [bold]committed to git[/]
                so every team member and container gets the same rules.
            """).strip(),
            title="[bold]Important Limitation[/]",
            border_style="yellow",
            expand=False,
            padding=(1, 2),
        )

        side_by_side = Table(box=None, show_header=False, padding=(0, 2))
        side_by_side.add_column(vertical="top")
        side_by_side.add_column(vertical="top")
        side_by_side.add_row(layer_table, Padding(limitation_panel, (1, 0, 0, 0)))
        console.print(Padding(side_by_side, (0, 2)))
        console.print()

        if config.enable_deny_list or config.enable_pretool_hook:
            console.print("  [green]✓[/] Your settings.json was generated in Step 7")
            console.print(f"    [dim]{config.install_dir}/settings.json[/]")
        else:
            console.print("  [dim]Deny list and PreToolUse hook were not enabled in Step 6.[/]")
            console.print("  [dim]You can go back to enable them, or set them up manually later.[/]")
        console.print()

        choices = []
        if config.enable_deny_list or config.enable_pretool_hook:
            choices.append(("Copy settings to a project now", "copy"))

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu(choices, initial_nav=nav)

        if result == "back":
            return "back"
        if result == "next":
            config.mark_complete(9)
            return "next"
        # result == "copy"
        _copy_project_settings(config)
        pause()


# ══════════════════════════════════════════════════════════════════
#  STEP 12 — BRANCH PROTECTION
# ══════════════════════════════════════════════════════════════════

def step_branch_protection(config: SetupConfig, came_from="next"):
    """Guide through setting up GitHub branch rulesets."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(13, TOTAL_STEPS, "Branch Protection",
                         "Set up GitHub branch rulesets to block destructive pushes")

        show_info_box("Why Branch Protection?", textwrap.dedent("""
            Even if every other layer fails, branch protection enforces
            rules [bold]server-side on GitHub[/]. Claude physically cannot:
            • Push directly to main (requires a PR)
            • Force-push to protected branches
            • Delete protected branches
        """).strip())

        console.print()

        # ── Recommended: one-click JSON import ──
        ruleset_path = os.path.join(config.install_dir, "branch-ruleset.json")
        show_info_box("Recommended — Import the Included Ruleset", textwrap.dedent(f"""
            A ready-to-use ruleset is included with your generated files:
            [cyan]{ruleset_path}[/]

            [bold]1.[/] Go to your repo → [cyan]Settings → Rules → Rulesets[/]
            [bold]2.[/] Click [bold]New ruleset → Import a ruleset[/]
            [bold]3.[/] Upload [cyan]branch-ruleset.json[/]
            [bold]4.[/] Review the settings and click [bold]Create[/]

            Repeat for each repo the PAT has access to.
        """).strip(), style="green")

        console.print()

        # What's in the ruleset
        rule_table = Table(title="What the Ruleset Configures", box=box.ROUNDED,
                           border_style="cyan")
        rule_table.add_column("Rule", style="bold")
        rule_table.add_column("Effect")
        rule_table.add_row("Target", 'Default branch (main/master)')
        rule_table.add_row("Restrict deletions", "Cannot delete the protected branch")
        rule_table.add_row("Block force pushes", "Cannot rewrite history on protected branch")
        rule_table.add_row("Require pull request", "PR required, 0 approvals (solo-friendly)")
        rule_table.add_row("Dismiss stale reviews", "Re-approval required after new commits")
        rule_table.add_row("Bypass actors", "None — rules apply to everyone")

        # Optional extras (manual)
        opt_table = Table(title="Optional — Add Manually After Import", box=box.ROUNDED,
                          border_style="dim cyan")
        opt_table.add_column("Setting", style="bold")
        opt_table.add_column("When to Enable")
        opt_table.add_row("Required approvals → 1+", "If working in a team (recommended)")
        opt_table.add_row("Require status checks", "If you have CI/CD tests configured")
        opt_table.add_row("Require linear history", "Team preference — cleaner git log")
        opt_table.add_row("Require signed commits", "High security environments only")

        side_by_side = Table(box=None, show_header=False, padding=(0, 2))
        side_by_side.add_column(vertical="top")
        side_by_side.add_column(vertical="top")
        side_by_side.add_row(rule_table, opt_table)
        console.print(Padding(side_by_side, (0, 2)))

        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([], initial_nav=nav)

        if result == "back":
            return "back"
        config.mark_complete(10)
        return "next"


# ══════════════════════════════════════════════════════════════════
#  STEP 13 — INSTALL GLOBAL LAUNCHER
# ══════════════════════════════════════════════════════════════════

def step_install_launcher(config: SetupConfig, came_from="next"):
    """Offer to install the alcatraz wrapper to PATH for easy access."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(14, TOTAL_STEPS, "Install Global Launcher",
                         "Add the 'alcatraz' command to your PATH for easy access")

        wrapper_src = os.path.join(config.install_dir, "alcatraz")
        home = os.path.expanduser("~")
        local_bin = os.path.join(home, ".local", "bin")
        wrapper_dest = os.path.join(local_bin, "alcatraz")

        console.print("  [bold]The wizard generated an [cyan]alcatraz[/cyan] launcher script.[/]")
        console.print()
        console.print("  Instead of typing the full path each time:")
        console.print(f"    [dim]{config.install_dir}/run.sh /path/to/project[/]")
        console.print()
        console.print("  You can just run:")
        console.print("    [cyan]alcatraz /path/to/project[/]")
        console.print("    [cyan]alcatraz[/]                       [dim]# Uses current directory[/]")
        console.print("    [cyan]alcatraz . fixed none[/]          [dim]# Fixed ports, offline[/]")
        console.print()

        # Check current state
        already_installed = os.path.exists(wrapper_dest) or shutil.which("alcatraz") is not None
        if already_installed:
            existing = shutil.which("alcatraz") or wrapper_dest
            console.print(f"  [green]✓[/] [cyan]alcatraz[/] is already on your PATH: [dim]{existing}[/]")
            console.print()

        choices = []
        if not already_installed:
            choices.append(("Attach symlink", "install"))
        else:
            choices.append(("Reinstall / update symlink", "install"))

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu(choices, initial_nav=nav)

        if result == "back":
            return "back"

        if result == "install":
            _do_install_launcher(config, wrapper_src, local_bin, wrapper_dest)

        return "next"


def _do_install_launcher(config: SetupConfig, wrapper_src: str, local_bin: str, wrapper_dest: str):
    """Perform the actual symlink + PATH setup."""
    home = os.path.expanduser("~")

    # 1. Ensure ~/.local/bin exists
    os.makedirs(local_bin, exist_ok=True)

    # 2. Create or update the symlink
    try:
        if os.path.lexists(wrapper_dest):
            os.remove(wrapper_dest)
        os.symlink(wrapper_src, wrapper_dest)
        console.print(f"  [green]✓[/] Symlinked [cyan]{wrapper_dest}[/] → [cyan]{wrapper_src}[/]")
    except OSError as e:
        console.print(f"  [yellow]⚠[/]  Could not create symlink: {e}")
        console.print(f"    [dim]Try manually: ln -sf {wrapper_src} {wrapper_dest}[/]")
        pause()
        return

    # 3. Ensure ~/.local/bin is in PATH
    if local_bin in os.environ.get("PATH", "").split(os.pathsep):
        console.print(f"  [green]✓[/] [cyan]~/.local/bin[/] is already in PATH")
    else:
        # Detect shell config files
        shell_configs = []
        for rc in [".bashrc", ".zshrc", ".profile"]:
            rc_path = os.path.join(home, rc)
            if os.path.isfile(rc_path):
                shell_configs.append(rc_path)

        if not shell_configs:
            # Fall back to .bashrc
            shell_configs = [os.path.join(home, ".bashrc")]

        path_line = '\nexport PATH="$HOME/.local/bin:$PATH"\n'
        for rc_path in shell_configs:
            # Check if already present
            try:
                content = open(rc_path).read()
                if '.local/bin' in content:
                    console.print(f"  [green]✓[/] PATH entry already in [cyan]{rc_path}[/]")
                    continue
            except FileNotFoundError:
                pass

            with open(rc_path, "a") as f:
                f.write(path_line)
            console.print(f"  [green]✓[/] Added PATH entry to [cyan]{rc_path}[/]")

        console.print()
        console.print("  [yellow]Note:[/] Run [cyan]source ~/.bashrc[/] (or restart your terminal)")
        console.print("  for the PATH change to take effect.")

    # 4. Verify
    console.print()
    console.print("  [bold]After restarting your terminal, you can run:[/]")
    console.print("    [cyan]alcatraz /path/to/project[/]")
    console.print()
    pause()


# ══════════════════════════════════════════════════════════════════
#  STEP 14 — DAILY WORKFLOW & COMPLETE
# ══════════════════════════════════════════════════════════════════

def step_daily_workflow(config: SetupConfig, came_from="next"):
    """Show daily workflow patterns and complete the wizard."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(15, TOTAL_STEPS, "Daily Workflow",
                         "How to use Claude Code safely every day")

        console.print("  [bold underline]Starting a Session[/]")
        console.print()
        console.print("    [cyan]cd ~/projects/my-project[/]")
        console.print("    [cyan]git checkout -b claude/feature-xyz[/]   [dim]# Always use a branch[/]")
        console.print("    [cyan]alcatraz[/]                              [dim]# Launch on current dir[/]")
        console.print()

        console.print("  [bold underline]After a Session[/]")
        console.print()
        console.print("    [cyan]git log --oneline -10[/]       [dim]# Review what Claude committed[/]")
        console.print("    [cyan]git diff main..HEAD[/]         [dim]# Full diff against main[/]")
        console.print("    [cyan]gh pr create --base main[/]    [dim]# Open a PR for review[/]")
        console.print()

        console.print("  [bold underline]Secrets Safety[/]")
        console.print()
        console.print("    The mounted project directory is readable by Claude.")
        console.print("    Don't store production secrets in the repo.")
        console.print("    Add [cyan].env[/] to [cyan].gitignore[/]. Use a secrets manager for production keys.")
        console.print()

        console.print("  [bold underline]Token Rotation[/]")
        console.print()
        console.print("    Rotate your GitHub PAT every [bold]30–90 days[/].")
        console.print("    Delete the old token on GitHub, create a new one,")
        console.print("    then update [cyan]~/.alcatraz-token[/].")
        console.print()

        # Port mode info
        if config.port_mode == "deterministic":
            show_info_box("Parallel Containers — Deterministic Ports", textwrap.dedent(f"""
                Ports are [bold]hash-based[/] — each project always gets the same unique
                host ports, so you can run multiple containers simultaneously.

                The banner at launch shows the exact mapping, e.g.:
                  [cyan]3000 → localhost:37593[/]

                Pass [cyan]fixed[/] to get 1:1 port mapping (single container only):
                  [cyan]alcatraz fixed[/]
            """).strip())
            console.print()

        # Final completion panel
        console.print(Panel(
            Align.center(
                Text("Setup Complete!\n\n", style="bold green", justify="center") +
                Text("Your Alcatraz environment is fully configured.\n", style="white", justify="center") +
                Text("All safety layers are in place:\n\n", style="white", justify="center") +
                Text("Docker isolation + PAT scoping + Git Guardian\n", style="cyan", justify="center") +
                Text("Branch protection + Deny list + PreToolUse hook\n", style="cyan", justify="center")
            ),
            border_style="green",
            box=box.DOUBLE,
            padding=(1, 4),
        ))

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        result = step_menu([], initial_nav=nav, continue_label="Exit")

        if result == "back":
            return "back"
        config.mark_complete(11)
        return "next"


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def step_preflight(config: SetupConfig, came_from="next"):
    """Navigable wrapper around run_preflight. Must pass to continue."""
    clear_screen()
    show_banner()
    if not run_preflight(config):
        console.print("\n  [bold red]Fix the issues above and try again.[/]\n")
        sys.exit(1)
    return "next"


def main():
    config = SetupConfig()

    clear_screen()
    show_banner()

    show_info_box("Disclaimer", textwrap.dedent("""
        [bold yellow]This is a hobby project and a work in progress.[/]

        While this setup implements multiple security layers (Docker sandboxing,
        Git Guardian, permission controls, resource limits), it [bold]does not
        guarantee Claude's safety[/] or prevent all possible misuse.

        [bold]--dangerously-skip-permissions[/] is used intentionally — the Docker
        container itself acts as the sandbox. Understand the trade-offs before using.

        [bold]You are responsible[/] for reviewing Claude's actions, using branch
        protection, and never blindly trusting autonomous operations.

        By continuing, you acknowledge these limitations and agree to use this
        setup responsibly.
    """).strip(), style="yellow")
    pause()

    # All steps support back navigation
    steps = [
        step_preflight,             # 0  — Step 1:  Pre-flight checks
        step_install_dir,           # 1  — Step 2:  Installation directory
        step_profile,               # 2  — Step 3:  Profile selection
        step_git_guardian,          # 3  — Step 4:  Git Guardian config
        step_network,               # 4  — Step 5:  Network & ports
        step_security,              # 5  — Step 6:  Security & auth
        step_review_and_generate,   # 6  — Step 7:  Review & generate files
        step_github_pat_creation,   # 7  — Step 8:  GitHub PAT creation
        step_token_storage,         # 8  — Step 9:  Store token
        step_docker_build,          # 9  — Step 10: Build Docker image
        step_claude_auth,           # 10 — Step 11: Claude authentication
        step_project_settings,      # 11 — Step 12: Project settings (.claude/)
        step_branch_protection,     # 12 — Step 13: Branch protection
        step_install_launcher,      # 13 — Step 14: Install global launcher
        step_daily_workflow,        # 14 — Step 15: Daily workflow & complete
    ]

    current = 0
    came_from = "next"
    # Clear debug log at start
    with open(_DEBUG_LOG, "w") as f:
        f.write("=== Wizard debug trace ===\n")
    while current < len(steps):
        _dbg(f"\n[MAIN] current={current}, calling {steps[current].__name__}, came_from={came_from}")
        # step_preflight handles its own clear_screen/show_banner.
        # step_review_and_generate (idx 6) needs it done here since it doesn't loop.
        if current == 6:
            clear_screen()
            show_banner()
        result = steps[current](config, came_from=came_from)
        _dbg(f"[MAIN] {steps[current].__name__} returned: {result!r}")
        if result == "back":
            old = current
            current = max(0, current - 1)
            came_from = "back"
            _dbg(f"[MAIN] BACK: {old} -> {current}")
        else:
            old = current
            current += 1
            came_from = "next"
            _dbg(f"[MAIN] NEXT: {old} -> {current}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n  [yellow]Cancelled by user.[/]\n")
        sys.exit(0)
    except questionary.ValidationError:
        console.print("\n\n  [red]Input error. Please try again.[/]\n")
        sys.exit(1)
