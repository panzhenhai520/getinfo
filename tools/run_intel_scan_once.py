#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Compatibility entry point for a manual one-shot intelligence scan."""

from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from intel_light_scanner import main


if __name__ == "__main__":
    raise SystemExit(main())
