"""Pre-flight checks and install mode selection for the Alcatraz Setup Wizard."""

import os
import sys
import subprocess
import platform
import textwrap
import re

from shared import console, q_style, TOTAL_STEPS, RECOMMENDED_TOTAL_STEPS
from config import SetupConfig
from ui import clear_screen, show_banner, show_check, show_info_box, pause

import questionary
from rich.prompt import Prompt


# ══════════════════════════════════════════════════════════════════
#  PRE-FLIGHT CHECKS
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
        return (False, "docker ps timed out after 30s -- daemon may be starting")


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
            # Already has section -- inject options line after the header
            patched = re.sub(
                r'(\[automount\])',
                r'\1\noptions = "metadata"',
                existing,
                count=1,
                flags=re.IGNORECASE,
            )
            result = subprocess.run(
                ["sudo", "tee", conf_path],
                input=patched, capture_output=True, text=True,
            )
        else:
            # No [automount] section -- append the whole block
            result = subprocess.run(
                ["sudo", "tee", "-a", conf_path],
                input=automount_block, capture_output=True, text=True,
            )
        return result.returncode == 0
    except Exception:
        return False


def _choose_install_mode(config: SetupConfig):
    """Let the user pick Recommended Install or Custom Install."""
    console.print()
    console.print("  [bold]How would you like to configure Alcatraz?[/]")
    console.print()

    choice = questionary.select(
        "",
        choices=[
            questionary.Choice(
                "Recommended Install",
                value="recommended",
            ),
            questionary.Choice(
                "Custom Install",
                value="custom",
            ),
        ],
        default="recommended",
        style=q_style,
    ).ask()
    if choice is None:
        sys.exit(0)
    config.install_mode = choice


def run_preflight(config: SetupConfig) -> bool:
    from rich.rule import Rule

    console.print()
    console.print(Rule(style="dim cyan"))
    console.print("  [bold cyan]Pre-Flight Checks[/]")
    console.print("  [dim]Verifying required tools are installed[/]")
    console.print(Rule(style="dim cyan"))
    console.print()

    os_type, is_wsl = detect_os()
    config.os_type = os_type
    config.is_wsl = is_wsl

    console.print(f"  [bold]Detected OS:[/] {os_type.upper()}" +
                  (" (Windows Subsystem for Linux)" if is_wsl else ""))
    console.print()

    all_ok = True
    critical_missing = []

    # -- Required tools --
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

    # These are informational -- they get installed in the Docker image
    node_ok, node_ver = check_command("node")
    show_check("Node.js (host)", node_ok, node_ver if node_ok else "Not needed on host")

    python_ok, python_ver = check_command("python3")
    show_check("Python 3 (host)", python_ok, python_ver if python_ok else "Not needed on host")

    gh_ok, gh_ver = check_command("gh")
    show_check("GitHub CLI (host)", gh_ok, gh_ver if gh_ok else "Not needed on host")

    # -- WSL-specific checks --
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
                    console.print("    [green]\u2713 Updated /etc/wsl.conf with metadata option.[/]")
                    console.print("    [cyan]Restart WSL to apply: close this terminal, then in PowerShell run:[/]")
                    console.print("      [bold cyan]wsl --shutdown[/]")
                    console.print("    [cyan]Then reopen your WSL terminal and re-run setup.[/]")
                else:
                    console.print("    [red]\u2717 Could not update /etc/wsl.conf. Apply manually:[/]")
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

    # -- Summary --
    console.print()
    if all_ok:
        show_info_box("All Clear", "[bold green]All required tools are installed and running.[/]\nReady to proceed with setup.", style="green")
        _choose_install_mode(config)
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
                console.print("  [bold green]\u2714[/] Docker is now running!")
                console.print()
                show_info_box("All Clear", "[bold green]All required tools are installed and running.[/]\nReady to proceed with setup.", style="green")
                _choose_install_mode(config)
                return True
            else:
                console.print("  [bold red]\u2718[/] Docker still not responding.")
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


def step_preflight(config: SetupConfig, came_from="next"):
    """Navigable wrapper around run_preflight. Must pass to continue."""
    clear_screen()
    show_banner()
    if not run_preflight(config):
        console.print("\n  [bold red]Fix the issues above and try again.[/]\n")
        sys.exit(1)
    return "next"
