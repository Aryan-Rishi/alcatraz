#!/usr/bin/env python3
"""
Alcatraz Setup Wizard — Entry Point
====================================
Bootstrap via: ./install.sh

All logic lives in the alcatraz/ package.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from main import main

if __name__ == "__main__":
    main()
