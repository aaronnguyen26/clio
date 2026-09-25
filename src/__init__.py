"""Autonomous Desktop Companion package root."""

import os
import sys

# Multi-layer zero-bytecode suppression: prevent .pyc generation from corrupting signed macOS bundle
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
