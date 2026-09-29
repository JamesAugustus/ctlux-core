# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Resolve the Radiance settings path in one place.

The data root is CTLUX_DATA_DIR, or the source root when unset. In either case,
the settings file is settings/settings.json under that root. Import creates no directories,
does not search personal data directories and only reads the settings file.
"""
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]


DATA_ROOT = Path(os.environ.get('CTLUX_DATA_DIR') or SOURCE_ROOT).resolve()
SETTINGS_FILE = DATA_ROOT / 'settings' / 'settings.json'
