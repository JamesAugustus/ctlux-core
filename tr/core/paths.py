# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Radiance ayar dosyası nerede, tek yerden karar verilir.

Veri kökü CTLUX_DATA_DIR, verilmemişse kaynak kökü. Ayar dosyası her iki durumda
da veri kökünün altında ayarlar/ayar.json. Import sırasında klasör açılmaz,
kişisel veri dizini kendiliğinden aranmaz, ayar dosyası sadece okunur.
"""
import os
from pathlib import Path

KOD_KOKU = Path(__file__).resolve().parents[1]


VERI_KOKU = Path(os.environ.get('CTLUX_DATA_DIR') or KOD_KOKU).resolve()
AYAR_DOSYASI = VERI_KOKU / 'ayarlar' / 'ayar.json'
