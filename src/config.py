"""SetupConfig dataclass and input validation for the Alcatraz Setup Wizard."""

import re
from dataclasses import dataclass, field

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
    # Install mode
    install_mode: str = "custom"  # recommended | custom
    step_display_overrides: dict = field(default_factory=dict)
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
        # Post-generation steps (5+6 tracked separately within combined GitHub Token step)
        5: "GitHub PAT",
        6: "Token Storage",
        7: "Docker Build",
        8: "Claude Auth",
        9: "Project Settings",
        10: "Branch Protection",
        11: "Daily Workflow",
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
