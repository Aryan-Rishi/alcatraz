"""Step functions for the Alcatraz Setup Wizard."""

import os
import sys
import re
import subprocess
import shutil
import textwrap
import time
import threading
import queue

from shared import (
    console, q_style, VERSION, TOTAL_STEPS, RECOMMENDED_TOTAL_STEPS, _dbg,
    Panel, Table, Text, Columns, Padding, Align, box, Live,
)
from config import SetupConfig, validate_branch_name, validate_memory_limit, validate_port
from ui import clear_screen, show_banner, show_step_header, show_info_box, show_check, pause, step_menu
from generators import _generate_files

import questionary


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
        # Use correct total based on install mode (set during preflight)
        total = RECOMMENDED_TOTAL_STEPS if config.install_mode == "recommended" else TOTAL_STEPS
        show_step_header(1, total, "Installation Directory",
                         "Where to create the alcatraz setup files")

        # On WSL, hint that ~/... paths live in the WSL filesystem, not the Windows drive
        if config.is_wsl:
            console.print("  [dim]Tip: On WSL, paths under ~ (e.g. ~/alcatraz) are stored in the WSL\n"
                          "  filesystem, not your Windows drive. To keep files on the Windows drive,\n"
                          "  use /mnt/c/Users/YourName/... instead.[/]")
            console.print()

        # Show current value
        current = config.install_dir or default_dir
        status = "[green]\u2714[/]" if config.is_step_complete(0) else "[dim]\u25cb[/]"
        console.print(f"  {status} [bold]Directory:[/] {current}")
        console.print()

        nav = 1 if first_iter and came_from == "back" else 0
        first_iter = False
        continue_label = "Install" if config.install_mode == "recommended" else "Continue"
        result = step_menu([("Edit directory", "edit")], initial_nav=nav, continue_label=continue_label)

        if result == "back":
            return result
        if result == "next":
            # Accept default if not yet edited
            if not config.install_dir:
                config.install_dir = default_dir
            config.mark_complete(0)
            if config.install_mode == "recommended":
                return _recommended_generate_and_build(config)
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
                console.print(f"  [red]\u2717  Cannot install to system directory: {abs_install}[/]")
                console.print(f"  [dim]Choose a path under your home directory instead.[/]")
                valid = False
                break

        # Warn if install path is outside home directory
        if valid and not abs_install.startswith(home_dir):
            console.print(f"  [yellow]\u26a0  Warning: directory is outside your home folder ({home_dir}).[/]")
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


# ── Recommended-mode generate & build (inline in Step 1) ─────────

def _recommended_generate_and_build(config: SetupConfig):
    """Recommended Install: show defaults, generate files, and build Docker within Step 1."""
    clear_screen()
    show_banner()
    show_step_header(1, RECOMMENDED_TOTAL_STEPS, "Installation Directory",
                     "Where to create the alcatraz setup files")

    console.print(f"  [green]\u2714[/] [bold]Directory:[/] {config.install_dir}")
    console.print()

    # Bulk-mark config steps as complete (using dataclass defaults)
    for i in range(5):
        config.mark_complete(i)

    # Show defaults summary
    console.print("  [bold]Using recommended defaults:[/]")
    console.print(f"    [bold]Profile:[/]       Recommended (~4-5 GB)")
    console.print(f"    [bold]Network:[/]       Bridge, deterministic ports (3000, 3001, 5173, 8080)")
    console.print(f"    [bold]Security:[/]      Deny list + PreToolUse hook")
    console.print(f"    [bold]Git Guardian:[/]  Protecting main, master, develop, production, release")
    console.print(f"    [bold]PAT type:[/]      Fine-grained")
    console.print()

    # Generate files
    _generate_files(config)

    # Build Docker image
    console.print("  [bold]Building Docker image[/] [dim](this may take 10-15 minutes)...[/]")
    console.print()

    if _run_docker_build(config):
        console.print()
        return step_menu([], initial_nav=0)

    # Build failed -- let user retry or skip
    console.print()
    action = questionary.select(
        "",
        choices=[
            questionary.Choice("Retry build", value="retry"),
            questionary.Choice("Skip (run ./build.sh manually later)", value="skip"),
        ],
        style=q_style,
    ).ask()
    if action is None:
        sys.exit(0)
    if action == "retry":
        if _run_docker_build(config):
            console.print()
            return step_menu([], initial_nav=0)
    console.print()
    console.print("  [dim]Run ./build.sh from your install directory when ready.[/]")
    console.print()
    return step_menu([], initial_nav=0)


def step_profile(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(2, TOTAL_STEPS, "Setup Profile",
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

        star = lambda p: "\u2b50 " if config.profile == p else "   "
        rows = [
            (f"{star('recommended')}Recommended", "~4\u20135 GB",
             "Core tools, GitHub CLI, Cloud CLIs, Infra, DB clients",
             "Most web/backend teams"),
            (f"{star('minimal')}Minimal", "~1.5\u20132 GB",
             "Core tools, GitHub CLI only",
             "Quick start, limited disk"),
            (f"{star('full')}Full", "~7\u20138 GB",
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
        status = "[green]\u2714[/]" if config.is_step_complete(1) else "[dim]\u25cb[/]"
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
        sel = lambda p: "\u2b50" if config.profile == p else "  "
        profile = questionary.select(
            "Select a profile:",
            choices=[
                questionary.Choice(f"{sel('recommended')} Recommended  \u2014 Core + Cloud + Infra + DB", value="recommended"),
                questionary.Choice(f"{sel('minimal')} Minimal      \u2014 Core + GitHub CLI only", value="minimal"),
                questionary.Choice(f"{sel('full')} Full         \u2014 Everything including ML packages", value="full"),
                questionary.Choice(f"{sel('custom')} Custom       \u2014 Choose each component", value="custom"),
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
            config.include_docker_cli = True
            config.include_github_cli = True
            config.include_db_clients = True
        elif profile == "minimal":
            config.include_cloud_clis = False
            config.include_infra_tools = False
            config.include_ml_packages = False
            config.include_docker_cli = False
            config.include_github_cli = True
            config.include_db_clients = False
        elif profile == "full":
            config.include_cloud_clis = True
            config.include_infra_tools = True
            config.include_ml_packages = True
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
            questionary.Choice("GitHub CLI (gh) \u2014 open PRs, check CI", value="github_cli", checked=config.include_github_cli),
            questionary.Choice("Docker CLI \u2014 build/run containers from inside", value="docker_cli", checked=config.include_docker_cli),
            questionary.Choice("Cloud CLIs \u2014 AWS, GCP, Azure", value="cloud_clis", checked=config.include_cloud_clis),
            questionary.Choice("Infrastructure \u2014 Terraform, kubectl", value="infra_tools", checked=config.include_infra_tools),
            questionary.Choice("Database clients \u2014 SQLite, PostgreSQL, MySQL, Redis", value="db_clients", checked=config.include_db_clients),
            questionary.Choice("ML/Data Science \u2014 PyTorch (CPU), NumPy, Pandas, Jupyter", value="ml_packages", checked=config.include_ml_packages),
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
    config.include_ml_packages = "ml_packages" in components
    return "next"


# ══════════════════════════════════════════════════════════════════
#  STEP 3 — GIT GUARDIAN CONFIGURATION
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
            console.print(f"  [yellow]\u26a0  Skipping invalid branch name:[/] [bold]{b}[/] "
                          "(only alphanumeric, '.', '_', '/', '-', '*' allowed)")
    if not valid_branches:
        console.print("  [yellow]No valid branch names \u2014 using defaults.[/]")
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
        show_step_header(3, TOTAL_STEPS, "Git Guardian Configuration",
                         "Configure the safety wrapper around git commands")

        show_info_box("What is Git Guardian?", textwrap.dedent("""
            The Git Guardian sits between Claude and the real [bold]git[/] binary.
            Safe commands ([cyan]add, commit, diff, log[/]) pass through silently.
            Dangerous commands ([red]force push, branch delete, hard reset[/]) pause
            and ask [bold]you[/] for confirmation in the terminal.
        """).strip())

        console.print()

        # Show current values
        status = "[green]\u2714[/]" if config.is_step_complete(2) else "[dim]\u25cb[/]"
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
            questionary.Choice("bridge (recommended) \u2014 Claude has internet access", value="bridge"),
            questionary.Choice("none \u2014 Fully offline, you push from host", value="none"),
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
                "deterministic (recommended) \u2014 Hash-based ports, parallel safe",
                value="deterministic"),
            questionary.Choice(
                "fixed \u2014 1:1 mapping (3000:3000), single container only",
                value="fixed"),
            questionary.Choice(
                "noports \u2014 No port forwarding at all",
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
            console.print("  [dim]Each project gets unique host ports via hashing \u2014 no conflicts.[/]")
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
                        console.print(f"  [yellow]\u26a0  Skipping invalid port:[/] {p} (must be 1-65535)")
                config.custom_ports = valid_ports

    config.mark_complete(3)


def step_network(config: SetupConfig, came_from="next"):
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(4, TOTAL_STEPS, "Network & Ports",
                         "Configure container networking and port forwarding")

        # Show current values
        status = "[green]\u2714[/]" if config.is_step_complete(3) else "[dim]\u25cb[/]"
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
#  STEP 5 — SECURITY & SAFETY LAYERS
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
            console.print("  [yellow]Invalid timeout \u2014 using default (4 hours).[/]")
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
            console.print(f"  [yellow]Invalid memory format '{mem_input}' \u2014 using default (8g).[/]")
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
                console.print(f"  [yellow]CPU count must be 1-32 \u2014 using default (4).[/]")
                config.resource_cpus = 4
            else:
                config.resource_cpus = val
        except (ValueError, TypeError):
            console.print("  [yellow]Invalid CPU count \u2014 using default (4).[/]")
            config.resource_cpus = 4

    # PAT type
    console.print()
    result = questionary.select(
        "GitHub PAT type you'll use:",
        choices=[
            questionary.Choice("Fine-grained (recommended) \u2014 scoped to specific repos", value="fine-grained"),
            questionary.Choice("Classic \u2014 broader access, use if org hasn't enabled fine-grained", value="classic"),
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
        show_step_header(5, TOTAL_STEPS, "Security & Safety Layers",
                         "Configure additional protection layers")

        # Show current values
        status = "[green]\u2714[/]" if config.is_step_complete(4) else "[dim]\u25cb[/]"
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


def step_review_and_generate(config: SetupConfig, came_from="next"):
    show_step_header(6, TOTAL_STEPS, "Review & Generate",
                     "Confirm your choices and generate all configuration files")

    # ── Validation gate — check all steps are complete ──
    missing = config.incomplete_steps()
    if missing:
        console.print("  [bold red]\u26a0  Cannot generate \u2014 the following steps are incomplete:[/]\n")
        for idx in missing:
            console.print(f"    [red]\u25cb[/] Step {idx + 1}: {config.STEP_NAMES[idx]}")
        console.print()
        console.print("  [dim]Go back and complete the incomplete steps, then return here.[/]")
        console.print()

        action = questionary.select(
            "",
            choices=[
                questionary.Choice("\u2190 Go back to complete steps", value="__back__"),
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
    console.print(f"  [cyan]  \u251c\u2500\u2500 Dockerfile[/]")
    console.print(f"  [cyan]  \u251c\u2500\u2500 git-guardian.sh[/]")
    console.print(f"  [cyan]  \u251c\u2500\u2500 run.sh[/]")
    console.print(f"  [cyan]  \u251c\u2500\u2500 alcatraz[/]              [dim](launcher \u2014 add to PATH)[/]")
    if config.enable_pretool_hook:
        console.print(f"  [cyan]  \u251c\u2500\u2500 pretool-hook.sh[/]")
    if config.enable_deny_list or config.enable_pretool_hook:
        console.print(f"  [cyan]  \u251c\u2500\u2500 settings.json[/]       [dim](copy to project .claude/)[/]")
    console.print(f"  [cyan]  \u251c\u2500\u2500 branch-ruleset.json[/] [dim](import into GitHub rulesets)[/]")
    console.print(f"  [cyan]  \u251c\u2500\u2500 build.sh[/]")
    console.print(f"  [cyan]  \u2514\u2500\u2500 auth.sh[/]              [dim](one-time OAuth login)[/]")
    console.print()

    action = questionary.select(
        "Ready to generate?",
        choices=[
            questionary.Choice("\u2713 Generate files with these settings", value="generate"),
            questionary.Choice("\u2190 Go back to change settings", value="__back__"),
        ],
        style=q_style,
    ).ask()

    if action is None:
        sys.exit(0)
    if action == "__back__":
        return "back"

    # ── Generate! ──
    _generate_files(config)
    pause()
    return "next"


# ══════════════════════════════════════════════════════════════════
#  RECOMMENDED STEP 2 — GENERATE FILES + DOCKER BUILD (legacy, now inline in Step 1)
# ══════════════════════════════════════════════════════════════════

def step_recommended_generate_and_build(config: SetupConfig, came_from="next"):
    """Recommended mode: generate all files with defaults, then build Docker image."""
    # Pass-through on back so user lands on step_install_dir (can switch modes)
    if came_from == "back":
        return "back"

    clear_screen()
    show_banner()
    show_step_header(2, RECOMMENDED_TOTAL_STEPS, "Generate & Build",
                     "Generate configuration files and build the Docker image")

    # Bulk-mark config steps 0-4 as complete (using dataclass defaults)
    for i in range(5):
        config.mark_complete(i)

    # Show defaults summary
    console.print("  [bold]Using recommended defaults:[/]")
    console.print(f"    [bold]Profile:[/]       Recommended (~4-5 GB)")
    console.print(f"    [bold]Network:[/]       Bridge, deterministic ports (3000, 3001, 5173, 8080)")
    console.print(f"    [bold]Security:[/]      Deny list + PreToolUse hook")
    console.print(f"    [bold]Git Guardian:[/]  Protecting main, master, develop, production, release")
    console.print(f"    [bold]PAT type:[/]      Fine-grained")
    console.print()

    # Generate files
    _generate_files(config)

    # Build Docker image
    console.print("  [bold]Building Docker image[/] [dim](this may take 10-15 minutes)...[/]")
    console.print()

    if _run_docker_build(config):
        console.print()
        pause()
        return "next"

    # Build failed -- let user retry or skip
    console.print()
    action = questionary.select(
        "",
        choices=[
            questionary.Choice("Retry build", value="retry"),
            questionary.Choice("Skip (run ./build.sh manually later)", value="skip"),
        ],
        style=q_style,
    ).ask()
    if action is None:
        sys.exit(0)
    if action == "retry":
        if _run_docker_build(config):
            console.print()
            pause()
            return "next"
    console.print()
    console.print("  [dim]Run ./build.sh from your install directory when ready.[/]")
    console.print()
    pause()
    return "next"


def step_github_pat_creation(config: SetupConfig, came_from="next"):
    """Guide the user through creating a GitHub Personal Access Token."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(7, TOTAL_STEPS, "Create GitHub Personal Access Token",
                         "Create a scoped token for secure Docker container access")

        if config.pat_type == "fine-grained":
            show_info_box("Fine-Grained PAT \u2014 Step by Step", textwrap.dedent("""
                [bold]1.[/] Go to [cyan]GitHub \u2192 Settings \u2192 Developer Settings \u2192 Fine-grained tokens[/]
                [bold]2.[/] Click [bold]Generate new token[/]
                [bold]3.[/] Configure:
                   [bold]Token name:[/]         alcatraz
                   [bold]Expiration:[/]         30\u201390 days (rotate regularly)
                   [bold]Resource owner:[/]     Your team org
                   [bold]Repository access:[/]  [cyan]Only select repositories[/] \u2014 pick repos you'll use
            """).strip())

            console.print()

            # Permissions table
            half_w = max((console.width - 8) // 2, 40)
            perm_table = Table(title="Permissions to Grant", box=box.ROUNDED,
                               border_style="green", width=half_w)
            perm_table.add_column("Permission", style="bold")
            perm_table.add_column("Level", style="cyan")
            perm_table.add_column("Required?")
            perm_table.add_row("Contents", "Read & Write", "Yes \u2014 push/pull code")
            perm_table.add_row("Metadata", "Read-only", "Yes \u2014 required by default")
            perm_table.add_row("Pull requests", "Read & Write", "Optional \u2014 open PRs")
            perm_table.add_row("Commit statuses", "Read & Write", "Optional \u2014 CI status checks")
            perm_table.add_row("Issues", "Read & Write", "Optional \u2014 create/close issues")
            perm_table.add_row("Actions", "Read-only", "Optional \u2014 check CI workflows")

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
            show_info_box("Classic PAT \u2014 Step by Step", textwrap.dedent("""
                [bold]1.[/] Go to [cyan]GitHub \u2192 Settings \u2192 Developer Settings \u2192 PATs (classic)[/]
                [bold]2.[/] Click [bold]Generate new token[/]
                [bold]3.[/] Configure:
                   [bold]Note:[/]        alcatraz
                   [bold]Expiration:[/]  30\u201390 days
                   [bold]Scopes:[/]      Only select [cyan]repo[/] (full control of private repos)

                [bold]4.[/] Do [bold red]NOT[/] select: delete_repo, admin:org, admin:repo_hook, gist
                [bold]5.[/] If your org uses SAML SSO, click [bold]"Configure SSO" \u2192 "Authorize"[/]
            """).strip())

            console.print()
            show_info_box("Classic PAT Limitation", textwrap.dedent("""
                [yellow]The repo scope grants access to ALL repos you can access,
                not just selected ones. This makes branch protection and
                Git Guardian even more important as compensating controls.[/]
            """).strip(), style="yellow")

        console.print()
        console.print("  [bold]Copy the token before leaving the page \u2014 you won't see it again.[/]")
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
        console.print("  [yellow]No token entered \u2014 skipping.[/]")
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
        console.print("  [yellow]Invalid date format \u2014 skipping expiry file.[/]")
        expiry = ""

    # Write token
    try:
        with open(token_path, "w") as f:
            f.write(token + "\n")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass  # NTFS via WSL may not support chmod
        console.print(f"  [green]\u2713[/] Token saved to [cyan]{token_path}[/]")

        if expiry:
            with open(expiry_path, "w") as f:
                f.write(expiry + "\n")
            console.print(f"  [green]\u2713[/] Expiry saved to [cyan]{expiry_path}[/]")

        config.mark_complete(6)
    except Exception as e:
        console.print(f"  [red]\u2717 Error writing token: {e}[/]")


def step_token_storage(config: SetupConfig, came_from="next"):
    """Collect and securely store the GitHub PAT."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(8, TOTAL_STEPS, "Store GitHub Token",
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
            console.print(f"  [green]\u2713[/] Token file exists: [cyan]{token_path}[/]")
            if expiry_str:
                console.print(f"    Expiry: [cyan]{expiry_str}[/]")
            console.print()
        else:
            console.print(f"  [dim]\u25cb[/] No token file found at [cyan]{token_path}[/]")
            console.print()

        show_info_box("What Happens", textwrap.dedent("""
            Your token is stored in [cyan]~/.alcatraz-token[/] with restricted
            permissions (chmod 600). The launch script reads it at runtime and
            injects it into the container as a root-owned credential \u2014 Claude
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
                console.print("  [bold red]\u26a0  Cannot continue \u2014 GitHub token not stored[/]")
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
#  RECOMMENDED STEP 2 — GITHUB TOKEN (PAT GUIDE + STORAGE)
# ══════════════════════════════════════════════════════════════════

def step_github_token_combined(config: SetupConfig, came_from="next"):
    """Combined PAT creation guide + token storage (used by both modes)."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        _sn, _st = config.step_display_overrides.get("github_token", (7, TOTAL_STEPS))
        show_step_header(_sn, _st, "GitHub Token",
                         "Create and store a GitHub Personal Access Token")

        home = os.path.expanduser("~")
        token_path = os.path.join(home, ".alcatraz-token")
        token_exists = os.path.isfile(token_path)

        # Always show PAT creation instructions
        show_info_box("Fine-Grained PAT -- Step by Step", textwrap.dedent("""
            [bold]1.[/] Go to [cyan]GitHub -> Settings -> Developer Settings -> Fine-grained tokens[/]
            [bold]2.[/] Click [bold]Generate new token[/]
            [bold]3.[/] Configure:
               [bold]Token name:[/]         alcatraz
               [bold]Expiration:[/]         30-90 days (rotate regularly)
               [bold]Resource owner:[/]     Your team org
               [bold]Repository access:[/]  [cyan]Only select repositories[/] -- pick repos you'll use
        """).strip())

        console.print()

        # Permissions table
        half_w = max((console.width - 8) // 2, 40)
        perm_table = Table(title="Permissions to Grant", box=box.ROUNDED,
                           border_style="green", width=half_w)
        perm_table.add_column("Permission", style="bold")
        perm_table.add_column("Level", style="cyan")
        perm_table.add_column("Required?")
        perm_table.add_row("Contents", "Read & Write", "Yes -- push/pull code")
        perm_table.add_row("Metadata", "Read-only", "Yes -- required by default")
        perm_table.add_row("Pull requests", "Read & Write", "Optional -- open PRs")
        perm_table.add_row("Commit statuses", "Read & Write", "Optional -- CI status checks")
        perm_table.add_row("Issues", "Read & Write", "Optional -- create/close issues")
        perm_table.add_row("Actions", "Read-only", "Optional -- check CI workflows")

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

        console.print()
        console.print("  [bold]Copy the token before leaving the page -- you won't see it again.[/]")
        console.print()

        # Token status
        if token_exists:
            expiry_path = os.path.join(home, ".alcatraz-token-expiry")
            expiry_str = ""
            if os.path.isfile(expiry_path):
                try:
                    expiry_str = open(expiry_path).read().strip()
                except Exception:
                    pass
            console.print(f"  [green]\u2713[/] Token stored: [cyan]{token_path}[/]")
            if expiry_str:
                console.print(f"    Expiry: [cyan]{expiry_str}[/]")
            console.print()
        else:
            console.print(f"  [dim]\u25cb[/] No token stored yet")
            console.print()

        choices = []
        if token_exists:
            choices.append(("Replace existing token", "store"))
        else:
            choices.append(("Store token now", "store"))

        nav = 1 if first_iter and came_from == "back" else 0
        row = 0 if first_iter and came_from == "next" else None
        first_iter = False
        result = step_menu(choices, initial_nav=nav, initial_row=row)

        if result == "back":
            return "back"
        if result == "next":
            if not token_exists:
                console.print()
                console.print("  [bold red]Cannot continue -- GitHub token not stored[/]")
                console.print(f"    Required: [cyan]{token_path}[/]")
                console.print()
                console.print("  [dim]Use 'Store token now' to save your PAT first.[/]")
                pause()
                continue
            config.mark_complete(5)
            config.mark_complete(6)
            return "next"
        # result == "store"
        _store_token(config)


# ══════════════════════════════════════════════════════════════════
#  DOCKER BUILD HELPER
# ══════════════════════════════════════════════════════════════════

def _run_docker_build(config: SetupConfig):
    """Execute the Docker build with live output. Returns True on success."""
    term_h = shutil.get_terminal_size().lines
    output_max = max(5, min(term_h - 15, 25))

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

        output_q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                output_q.put(line)
            output_q.put(None)

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
                    pass
            proc.wait()

        if proc.returncode == 0:
            console.print()
            console.print("  [bold green]\u2713 Docker image built successfully![/]")
            config.mark_complete(7)
            return True
        else:
            console.print()
            console.print(f"  [bold red]x Build failed (exit code {proc.returncode})[/]")
            console.print("  [dim]Check the output above for errors, or re-run ./build.sh[/]")
            return False
    except KeyboardInterrupt:
        console.print("\n  [yellow]Build cancelled. Run ./build.sh manually later.[/]")
        return False
    except Exception as e:
        console.print(f"\n  [red]x Build error: {e}[/]")
        console.print("  [dim]Make sure Docker is running, then try ./build.sh manually.[/]")
        return False


# ══════════════════════════════════════════════════════════════════
#  STEP 8 — DOCKER BUILD
# ══════════════════════════════════════════════════════════════════

def step_docker_build(config: SetupConfig, came_from="next"):
    """Build the Docker image from the generated Dockerfile."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        _sn, _st = config.step_display_overrides.get("docker_build", (8, TOTAL_STEPS))
        show_step_header(_sn, _st, "Build Docker Image",
                         "Build the Alcatraz Docker image")

        # Estimate build time based on profile
        estimates = {
            "minimal": ("5\u20138 minutes", "~1.5\u20132 GB"),
            "recommended": ("10\u201315 minutes", "~4\u20135 GB"),
            "full": ("15\u201320 minutes", "~7\u20138 GB"),
            "custom": ("10\u201320 minutes", "varies"),
        }
        time_est, size_est = estimates.get(config.profile, ("10\u201315 minutes", "varies"))

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
                console.print("  [green]\u2713[/] Image [cyan]alcatraz:latest[/] already exists")
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
                console.print("  [bold red]\u26a0  Cannot continue \u2014 Docker image not built[/]")
                console.print("    Required: [cyan]alcatraz:latest[/]")
                console.print()
                console.print("  [dim]Use 'Build now' to create the Docker image first.[/]")
                pause()
                continue
            return "next"

        # result == "build"
        clear_screen()
        show_banner()
        _sn, _st = config.step_display_overrides.get("docker_build", (8, TOTAL_STEPS))
        show_step_header(_sn, _st, "Build Docker Image",
                         "Building the Alcatraz Docker image")
        console.print()
        console.print("  [dim]Ctrl+C to cancel (you can run ./build.sh later).[/]")
        console.print()

        if _run_docker_build(config):
            console.print()
            pause()
            return "next"

        console.print()
        pause()


def _run_oauth_auth(config: SetupConfig):
    """Execute auth.sh interactively for OAuth login."""
    clear_screen()
    show_banner()
    _sn, _st = config.step_display_overrides.get("claude_auth", (9, TOTAL_STEPS))
    show_step_header(_sn, _st, "Authenticate Claude Code",
                     "Running OAuth login...")
    console.print()
    console.print("  [dim]A browser window will open. Complete the login,\n"
                  "  then type [cyan]/exit[/cyan] in the terminal.[/dim]")
    console.print()

    auth_script = os.path.join(config.install_dir, "auth.sh")
    if not os.path.isfile(auth_script):
        console.print(f"  [bold red]\u2717 Auth script not found:[/] [cyan]{auth_script}[/]")
        console.print("  [dim]Go back to Step 6 (Review & Generate) to generate files first.[/]")
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
            console.print("  [bold green]\u2713 Authentication completed![/]")
        else:
            console.print()
            console.print(f"  [yellow]Auth script exited with code {proc.returncode}[/]")
            console.print(f"  [dim]You can retry or run manually: {auth_script}[/]")
    except KeyboardInterrupt:
        console.print(f"\n  [yellow]Auth cancelled. You can retry or run manually: {auth_script}[/]")
    except Exception as e:
        console.print(f"\n  [red]\u2717 Auth error: {e}[/]")
        console.print(f"  [dim]You can run manually: {auth_script}[/]")

    console.print()
    pause()


def step_claude_auth(config: SetupConfig, came_from="next"):
    """Guide through Claude Code OAuth authentication."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        _sn, _st = config.step_display_overrides.get("claude_auth", (9, TOTAL_STEPS))
        show_step_header(_sn, _st, "Authenticate Claude Code",
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
            console.print(f"  [green]\u2713[/] Already authenticated \u2014 credentials found at:")
            console.print(f"    [cyan]{check_path}[/]")
            console.print()
        else:
            console.print(f"  [dim]\u25cb[/] Not yet authenticated")
            console.print(f"    Expected: [cyan]{check_path}[/]")
            console.print()

        if config.auth_method == "oauth":
            show_info_box("OAuth Authentication", textwrap.dedent("""
                Claude Code authenticates via OAuth (browser-based login).

                This will open a browser window \u2014 complete the login,
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
        row = 0 if first_iter and came_from == "next" and choices else None
        first_iter = False
        result = step_menu(choices, initial_nav=nav, initial_row=row)

        if result == "back":
            return "back"
        if result == "next":
            if not already_auth:
                console.print()
                console.print("  [bold red]\u26a0  Cannot continue \u2014 Claude Code not authenticated[/]")
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
        console.print(f"  [red]\u2717 Directory not found: {project_dir}[/]")
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
        console.print(f"  [green]\u2713[/] Copied settings.json \u2192 [cyan]{dst_settings}[/]")
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
            console.print(f"  [green]\u2713[/] Copied pretool-hook.sh \u2192 [cyan]{dst_hook}[/]")

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
        show_step_header(10, TOTAL_STEPS, "Project Settings",
                         "Deploy safety rules to your project's .claude/ directory")

        show_info_box("What is .claude/settings.json?", textwrap.dedent("""
            Claude Code reads [cyan].claude/settings.json[/] from each project.
            It contains [bold]deny rules[/] that block dangerous commands
            [bold]before[/] they execute \u2014 even in --dangerously-skip-permissions mode.

            [bold]Evaluation order:[/] deny \u2192 ask \u2192 allow \u2192 permission mode
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
            console.print("  [green]\u2713[/] Your settings.json was generated in Step 6")
            console.print(f"    [dim]{config.install_dir}/settings.json[/]")
        else:
            console.print("  [dim]Deny list and PreToolUse hook were not enabled in Step 5.[/]")
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


def step_branch_protection(config: SetupConfig, came_from="next"):
    """Guide through setting up GitHub branch rulesets."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(11, TOTAL_STEPS, "Branch Protection",
                         "Set up GitHub branch rulesets to block destructive pushes")

        show_info_box("Why Branch Protection?", textwrap.dedent("""
            Even if every other layer fails, branch protection enforces
            rules [bold]server-side on GitHub[/]. Claude physically cannot:
            \u2022 Push directly to main (requires a PR)
            \u2022 Force-push to protected branches
            \u2022 Delete protected branches
        """).strip())

        console.print()

        # ── Recommended: one-click JSON import ──
        ruleset_path = os.path.join(config.install_dir, "branch-ruleset.json")
        show_info_box("Recommended \u2014 Import the Included Ruleset", textwrap.dedent(f"""
            A ready-to-use ruleset is included with your generated files:
            [cyan]{ruleset_path}[/]

            [bold]1.[/] Go to your repo \u2192 [cyan]Settings \u2192 Rules \u2192 Rulesets[/]
            [bold]2.[/] Click [bold]New ruleset \u2192 Import a ruleset[/]
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
        rule_table.add_row("Bypass actors", "None \u2014 rules apply to everyone")

        # Optional extras (manual)
        opt_table = Table(title="Optional \u2014 Add Manually After Import", box=box.ROUNDED,
                          border_style="dim cyan")
        opt_table.add_column("Setting", style="bold")
        opt_table.add_column("When to Enable")
        opt_table.add_row("Required approvals \u2192 1+", "If working in a team (recommended)")
        opt_table.add_row("Require status checks", "If you have CI/CD tests configured")
        opt_table.add_row("Require linear history", "Team preference \u2014 cleaner git log")
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


def step_install_launcher(config: SetupConfig, came_from="next"):
    """Offer to install the alcatraz wrapper to PATH for easy access."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(12, TOTAL_STEPS, "Install Global Launcher",
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
            console.print(f"  [green]\u2713[/] [cyan]alcatraz[/] is already on your PATH: [dim]{existing}[/]")
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
        console.print(f"  [green]\u2713[/] Symlinked [cyan]{wrapper_dest}[/] \u2192 [cyan]{wrapper_src}[/]")
    except OSError as e:
        console.print(f"  [yellow]\u26a0[/]  Could not create symlink: {e}")
        console.print(f"    [dim]Try manually: ln -sf {wrapper_src} {wrapper_dest}[/]")
        pause()
        return

    # 3. Ensure ~/.local/bin is in PATH
    if local_bin in os.environ.get("PATH", "").split(os.pathsep):
        console.print(f"  [green]\u2713[/] [cyan]~/.local/bin[/] is already in PATH")
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
                    console.print(f"  [green]\u2713[/] PATH entry already in [cyan]{rc_path}[/]")
                    continue
            except FileNotFoundError:
                pass

            with open(rc_path, "a") as f:
                f.write(path_line)
            console.print(f"  [green]\u2713[/] Added PATH entry to [cyan]{rc_path}[/]")

        console.print()
        console.print("  [yellow]Note:[/] Run [cyan]source ~/.bashrc[/] (or restart your terminal)")
        console.print("  for the PATH change to take effect.")

    # 4. Verify
    console.print()
    console.print("  [bold]After restarting your terminal, you can run:[/]")
    console.print("    [cyan]alcatraz /path/to/project[/]")
    console.print()
    pause()


def step_daily_workflow(config: SetupConfig, came_from="next"):
    """Show daily workflow patterns and complete the wizard."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(13, TOTAL_STEPS, "Daily Workflow",
                         "How to use Claude Code safely every day")

        console.print("  [bold underline]Starting a Session[/]")
        console.print()
        console.print("    [cyan]cd ~/projects/my-project[/]")
        console.print("    [cyan]git checkout -b claude/feature-xyz[/]   [dim]# Always use a branch[/]")
        console.print("    [cyan]git pull origin main[/]                  [dim]# Sync latest from main[/]")
        console.print("    [cyan]alcatraz[/]                              [dim]# Launch on current dir[/]")
        console.print()

        console.print("  [bold underline]After a Session[/]")
        console.print()
        console.print("    [cyan]git log --oneline -10[/]       [dim]# Review what Claude committed[/]")
        console.print("    [cyan]git diff main..HEAD[/]         [dim]# Full diff against main[/]")
        console.print("    [cyan]gh pr create --base main --head claude/feature-xyz --title \"PR title\" --body \" \"[/]  [dim]# Open a PR for review[/]")
        console.print()

        console.print("  [bold underline]Secrets Safety[/]")
        console.print()
        console.print("    The mounted project directory is readable by Claude.")
        console.print("    Don't store production secrets in the repo.")
        console.print("    Add [cyan].env[/] to [cyan].gitignore[/]. Use a secrets manager for production keys.")
        console.print()

        console.print("  [bold underline]Token Rotation[/]")
        console.print()
        console.print("    Rotate your GitHub PAT every [bold]30\u201390 days[/].")
        console.print("    Delete the old token on GitHub, create a new one,")
        console.print("    then update [cyan]~/.alcatraz-token[/].")
        console.print()

        # Port mode info
        if config.port_mode == "deterministic":
            show_info_box("Parallel Containers \u2014 Deterministic Ports", textwrap.dedent(f"""
                Ports are [bold]hash-based[/] \u2014 each project always gets the same unique
                host ports, so you can run multiple containers simultaneously.

                The banner at launch shows the exact mapping, e.g.:
                  [cyan]3000 \u2192 localhost:37593[/]

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
#  RECOMMENDED STEP 4 — FINALIZE (LAUNCHER + WORKFLOW + SECURITY NOTES)
# ══════════════════════════════════════════════════════════════════

def step_finalize_combined(config: SetupConfig, came_from="next"):
    """Recommended mode: install launcher, show workflow tips and security notes."""
    first_iter = True
    while True:
        clear_screen()
        show_banner()
        show_step_header(4, RECOMMENDED_TOTAL_STEPS, "Setup Complete",
                         "Launcher installation and next steps")

        # Auto-install launcher
        wrapper_src = os.path.join(config.install_dir, "alcatraz")
        home = os.path.expanduser("~")
        local_bin = os.path.join(home, ".local", "bin")
        wrapper_dest = os.path.join(local_bin, "alcatraz")

        already_installed = os.path.exists(wrapper_dest) or shutil.which("alcatraz") is not None
        if not already_installed:
            _do_install_launcher(config, wrapper_src, local_bin, wrapper_dest)
        else:
            existing = shutil.which("alcatraz") or wrapper_dest
            console.print(f"  [green]\u2713[/] [cyan]alcatraz[/] is already on your PATH: [dim]{existing}[/]")
        console.print()

        # Daily workflow
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
        console.print("    [cyan]gh pr create[/]                [dim]# Open a PR for review[/]")
        console.print()

        # Security notes for skipped steps
        ruleset_path = os.path.join(config.install_dir, "branch-ruleset.json")
        settings_path = os.path.join(config.install_dir, "settings.json")
        show_info_box("Recommended: Additional Security Steps", textwrap.dedent(f"""
            Recommended Install configured core security (deny list,
            PreToolUse hook, Git Guardian). For full protection,
            also set up:

            [bold]1. Branch Protection (GitHub Rulesets)[/]
               Import the included ruleset into your repo:
               Settings -> Rules -> Rulesets -> Import a ruleset
               File: [cyan]{ruleset_path}[/]

            [bold]2. Project Settings (.claude/settings.json)[/]
               Copy settings.json to each project's .claude/ dir:
               [cyan]cp {settings_path} /path/to/project/.claude/[/]
               This adds command-level deny rules per project.
        """).strip(), style="yellow")
        console.print()

        # Completion panel
        console.print(Panel(
            Align.center(
                Text("Setup Complete!\n\n", style="bold green", justify="center") +
                Text("Your Alcatraz environment is fully configured.\n", style="white", justify="center") +
                Text("All safety layers are in place:\n\n", style="white", justify="center") +
                Text("Docker isolation + PAT scoping + Git Guardian\n", style="cyan", justify="center") +
                Text("Deny list + PreToolUse hook\n", style="cyan", justify="center")
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
        return "next"
