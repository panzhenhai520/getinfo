#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Compatibility entry point for one manual candidate-dispatch batch."""

from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from candidate_dispatcher import main


if __name__ == "__main__":
    raise SystemExit(main())
