"""Main entry point and orchestration for the Alcatraz Setup Wizard."""

import sys
import textwrap

from shared import console, _dbg, _DEBUG_LOG, RECOMMENDED_TOTAL_STEPS
from config import SetupConfig
from ui import clear_screen, show_banner, show_info_box, pause
from preflight import step_preflight
from steps import (
    step_install_dir,
    step_profile,
    step_git_guardian,
    step_network,
    step_security,
    step_review_and_generate,
    step_github_token_combined,
    step_docker_build,
    step_claude_auth,
    step_project_settings,
    step_branch_protection,
    step_install_launcher,
    step_daily_workflow,
    step_finalize_combined,
)

import questionary


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

    # Step lists — both share indices 0-1; mode switch evaluated at index 2
    custom_steps = [
        step_preflight,             # 0  — Pre-flight checks (not numbered)
        step_install_dir,           # 1  — Step 1:  Installation directory
        step_profile,               # 2  — Step 2:  Profile selection
        step_git_guardian,          # 3  — Step 3:  Git Guardian config
        step_network,               # 4  — Step 4:  Network & ports
        step_security,              # 5  — Step 5:  Security & auth
        step_review_and_generate,   # 6  — Step 6:  Review & generate files
        step_github_token_combined, # 7  — Step 7:  GitHub token (PAT + storage)
        step_docker_build,          # 8  — Step 8:  Build Docker image
        step_claude_auth,           # 9  — Step 9:  Claude authentication
        step_project_settings,      # 10 — Step 10: Project settings (.claude/)
        step_branch_protection,     # 11 — Step 11: Branch protection
        step_install_launcher,      # 12 — Step 12: Install global launcher
        step_daily_workflow,        # 13 — Step 13: Daily workflow & complete
    ]

    recommended_steps = [
        step_preflight,                # 0  — Pre-flight checks (not numbered)
        step_install_dir,              # 1  — Step 1: Install dir + Generate & Build
        step_github_token_combined,    # 2  — Step 2: GitHub token (PAT + storage)
        step_claude_auth,              # 3  — Step 3: Claude authentication
        step_finalize_combined,        # 4  — Step 4: Launcher + workflow + notes
    ]

    steps = custom_steps
    current = 0
    came_from = "next"
    # Clear debug log at start
    try:
        with open(_DEBUG_LOG, "w") as f:
            f.write("=== Wizard debug trace ===\n")
    except OSError:
        pass
    while current < len(steps):
        # Mode switch at the boundary (index 2) — both lists share 0-1
        if current == 2:
            if config.install_mode == "recommended":
                steps = recommended_steps
                config.step_display_overrides = {
                    "github_token": (2, RECOMMENDED_TOTAL_STEPS),
                    "claude_auth": (3, RECOMMENDED_TOTAL_STEPS),
                }
            else:
                steps = custom_steps
                config.step_display_overrides = {}
                # Clear auto-marked steps if switching back from recommended
                for i in range(1, 5):
                    config.completed_steps.discard(i)

        _dbg(f"\n[MAIN] current={current}, calling {steps[current].__name__}, came_from={came_from}")
        # step_preflight handles its own clear_screen/show_banner.
        # step_review_and_generate (custom idx 6) needs it done here since it doesn't loop.
        if steps is custom_steps and current == 6:
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
