#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
#  Alcatraz Setup Wizard — Bootstrap Launcher
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    chmod +x install.sh && ./install.sh
#
#  This script:
#    1. Checks for Python 3.8+
#    2. Installs required Python packages (rich, questionary)
#    3. Launches the setup wizard TUI
#
# ═══════════════════════════════════════════════════════════════

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Banner ──
echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║       Alcatraz Setup Wizard  v1.1.0             ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Check Python ──
echo -e "  ${BOLD}Checking Python...${NC}"

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &> /dev/null; then
        # Check version >= 3.8 (|| true prevents set -e from exiting on Windows Store python3 stub)
        PY_VERSION=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        PY_MAJOR=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || true)
        PY_MINOR=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || true)
        if [ -n "$PY_MAJOR" ] && [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 8 ] 2>/dev/null; then
            PYTHON_CMD="$cmd"
            echo -e "  ${GREEN}✓${NC} Found $cmd $PY_VERSION"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "  ${RED}✗ Python 3.8+ is required but not found.${NC}"
    echo ""
    echo "  Install Python:"
    echo "    macOS:   brew install python3"
    echo "    Ubuntu:  sudo apt install python3 python3-pip"
    echo "    Windows: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

# ── Check/install dependencies ──
echo -e "  ${BOLD}Checking Python packages...${NC}"

MISSING_PKGS=()

$PYTHON_CMD -c "import rich" 2>/dev/null || MISSING_PKGS+=("rich")
$PYTHON_CMD -c "import questionary" 2>/dev/null || MISSING_PKGS+=("questionary")

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    PKGS_STR="${MISSING_PKGS[*]}"
    echo -e "  ${YELLOW}Installing: ${PKGS_STR}${NC}"

    # If pip isn't available, try to install it first
    if ! $PYTHON_CMD -m pip --version &> /dev/null; then
        echo -e "  ${YELLOW}⚠${NC} pip not found, attempting to install it..."
        if command -v apt &> /dev/null; then
            echo -e "  ${DIM}Running: sudo apt install -y python3-pip${NC}"
            if sudo apt install -y python3-pip 2>/dev/null; then
                echo -e "  ${GREEN}✓${NC} pip installed"
            fi
        elif command -v dnf &> /dev/null; then
            echo -e "  ${DIM}Running: sudo dnf install -y python3-pip${NC}"
            if sudo dnf install -y python3-pip 2>/dev/null; then
                echo -e "  ${GREEN}✓${NC} pip installed"
            fi
        elif command -v pacman &> /dev/null; then
            echo -e "  ${DIM}Running: sudo pacman -S --noconfirm python-pip${NC}"
            if sudo pacman -S --noconfirm python-pip 2>/dev/null; then
                echo -e "  ${GREEN}✓${NC} pip installed"
            fi
        fi
        # Last resort: try ensurepip
        if ! $PYTHON_CMD -m pip --version &> /dev/null; then
            $PYTHON_CMD -m ensurepip --upgrade 2>/dev/null || true
        fi
    fi

    # Try pip install with various methods
    if $PYTHON_CMD -m pip install --user --quiet ${MISSING_PKGS[@]} 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Packages installed"
    elif $PYTHON_CMD -m pip install --break-system-packages --user --quiet ${MISSING_PKGS[@]} 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Packages installed"
    elif $PYTHON_CMD -m pip install --quiet ${MISSING_PKGS[@]} 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Packages installed"
    elif $PYTHON_CMD -m pip install --break-system-packages --quiet ${MISSING_PKGS[@]} 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Packages installed"
    else
        echo -e "  ${RED}✗ Failed to install packages. Try manually:${NC}"
        echo ""
        echo "    $PYTHON_CMD -m pip install ${PKGS_STR}"
        echo ""
        exit 1
    fi
else
    echo -e "  ${GREEN}✓${NC} All packages present"
fi

# ── Launch wizard ──
echo ""
echo -e "  ${DIM}Launching setup wizard...${NC}"
echo ""

exec $PYTHON_CMD "$SCRIPT_DIR/setup.py"
