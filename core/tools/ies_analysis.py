#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
IES analysis: read a luminaire photometry file and produce an English summary.
Usage:    python3 ies_analysis.py file.ies [file2.ies ...]
Reports:  lumens, watts, efficacy (lm/W), peak candela, beam angle,
           symmetry, unit checks, estimated Kelvin, and Radiance warnings.
"""
import sys, os, re, math


def _numbers(text):
    # Skipping malformed numeric tokens would shift the header fields.
    values = [float(x) for x in re.split(r"[\s,]+", text.strip()) if x]
    if not all(math.isfinite(x) for x in values):
        raise ValueError("IES numeric body contains a nonfinite value")
    return values


def read_ies(path):
    with open(path, "rb") as f:
        raw = f.read()
    non_ascii = sum(1 for b in raw if b > 127)
    text = raw.decode("latin-1")
    lines = text.splitlines()

    keys = {}
    tilt_i = None
    for i, ln in enumerate(lines):
        m = re.match(r"\s*\[(\w+)\]\s*(.*)", ln)
        if m:
            keys[m.group(1).upper()] = m.group(2).strip()
        if ln.strip().upper().startswith("TILT="):
            tilt_i = i
            tilt = ln.strip()[5:].strip()
            break
    if tilt_i is None:
        raise ValueError("Missing TILT line. Not a valid IES file.")

    body = " ".join(lines[tilt_i + 1:])
    n = _numbers(body)
    if tilt.upper() == "INCLUDE":
        # skip the tilt block: lamp2tilt, pair count, n angles, n multipliers
        if len(n) < 2 or n[1] < 0 or not n[1].is_integer():
            raise ValueError('Malformed IES TILT table')
        quantity = int(n[1])
        if len(n) < 2 + 2 * quantity + 13:
            raise ValueError('Incomplete IES TILT table')
        n = n[2 + 2 * quantity:]
        # the initial lamp-to-luminaire value (n[0]) was also skipped
    elif tilt.upper() != 'NONE':
        raise ValueError('External IES TILT files are not supported')
    if len(n) < 13:
        raise ValueError("Photometric rows are missing/malformed.")

    lamp      = int(n[0]);  lumens_per_lamp = n[1];  multiplier = n[2]
    if any(x < 1 or not x.is_integer() for x in (n[3], n[4])):
        raise ValueError('IES angle counts must be positive integers')
    n_vertical    = int(n[3]);  n_horizontal     = int(n[4])
    photometry_type    = int(n[5]);  unit       = int(n[6])
    width, length, height = n[7], n[8], n[9]
    ballast     = n[10];      future     = n[11];  watt = n[12]

    remaining   = n[13:]
    vertical_angles = remaining[:n_vertical]
    horizontal_angles = remaining[n_vertical:n_vertical + n_horizontal]
    candela = remaining[n_vertical + n_horizontal:n_vertical + n_horizontal + n_vertical * n_horizontal]
    expected = n_vertical + n_horizontal + n_vertical * n_horizontal
    if len(remaining) != expected:
        raise ValueError('IES angle/candela table size mismatch: expected=%d, found=%d' %
                         (expected, len(remaining)))
    if multiplier < 0 or any(x < 0 for x in candela):
        raise ValueError('IES candela/multiplier cannot be negative')

    return {
        "file": os.path.basename(path), "keywords": keys,
        "lamp": lamp, "lumens_per_lamp": lumens_per_lamp, "multiplier": multiplier,
        "n_vertical": n_vertical, "n_horizontal": n_horizontal, "photometry_type": photometry_type,
        "unit": unit, "dimensions": (width, length, height),
        "ballast": ballast, "watt": watt,
        "vertical_angles": vertical_angles, "horizontal_angles": horizontal_angles, "candela": candela,
        "non_ascii": non_ascii,
    }


def beam_angle(vertical_angles, candela, n_vertical, n_horizontal):
    """Angle where the first horizontal plane falls to 50% of its peak -> beam angle."""
    if not candela or n_vertical == 0:
        return None, None
    plane = candela[:n_vertical]
    peak = max(plane)
    if peak <= 0:
        return None, peak
    half_peak = None
    for i in range(len(plane)):
        if plane[i] < peak / 2.0:
            if i == 0:
                half_peak = vertical_angles[0]
            else:
                # linear interpolation
                a0, a1 = vertical_angles[i - 1], vertical_angles[i]
                c0, c1 = plane[i - 1], plane[i]
                half_peak = a0 + (a1 - a0) * (c0 - peak / 2.0) / max(c0 - c1, 1e-9)
            break
    return (2 * half_peak if half_peak is not None else None), peak


def estimate_kelvin(v):
    sources = [v["file"]] + list(v["keywords"].values())
    for s in sources:
        m = re.search(r"(\d{4})\s*K", s, re.I)
        if m and 1500 <= int(m.group(1)) <= 10000:
            return int(m.group(1)), s
        m = re.search(r"\b(27|30|35|40|50|57|65)K\b", s, re.I)
        if m:
            return int(m.group(1)) * 100, s
    return None, None


def print_analysis(path):
    v = read_ies(path)
    A = v["keywords"]
    beam, peak = beam_angle(v["vertical_angles"], v["candela"], v["n_vertical"], v["n_horizontal"])
    kelvin, temperature_source = estimate_kelvin(v)

    absolute = v["lumens_per_lamp"] < 0
    total_lumens = None if absolute else v["lamp"] * v["lumens_per_lamp"] * (v["multiplier"] or 1)
    efficacy = (total_lumens / v["watt"]) if (total_lumens and v["watt"] > 0) else None

    max_vertical = max(v["vertical_angles"]) if v["vertical_angles"] else 0
    min_vertical  = min(v["vertical_angles"]) if v["vertical_angles"] else 0
    if max_vertical <= 90:
        distribution = "DOWNWARD emission (downlight/spot type)"
    elif min_vertical >= 90:
        distribution = "UPWARD emission (uplight)"
    else:
        distribution = "both downward and upward emission"

    symmetry = {1: "fully symmetric (single 0° plane)", }.get(
        v["n_horizontal"], "%d horizontal planes (may be asymmetric)" % v["n_horizontal"])
    if v["n_horizontal"] == 1:
        symmetry = "rotationally symmetric"

    print("=" * 62)
    print("IES SUMMARY: %s" % v["file"])
    print("=" * 62)
    for label, key in (("Manufacturer", "MANUFAC"), ("Product code", "LUMCAT"),
                            ("Luminaire", "LUMINAIRE"), ("Lamp", "LAMP"),
                            ("Lamp code", "LAMPCAT"), ("Test", "TEST")):
        if A.get(key):
            print("  %-12s: %s" % (label, A[key]))
    print("-" * 62)
    if absolute:
        print("  Lumens      : ABSOLUTE photometry (typical for LEDs). Lumens are in the candela table.")
    else:
        print("  Lumens      : %.0f lm  (%d lamps × %.0f lm)"
              % (total_lumens, v["lamp"], v["lumens_per_lamp"]))
    print("  Power       : %.1f W" % v["watt"] if v["watt"] > 0 else "  Power       : not specified in IES")
    if efficacy:
        print("  Efficacy    : %.0f lm/W" % efficacy)
    if kelvin:
        print("  Color temp. : ~%d K  (source: '%s')" % (kelvin, (temperature_source or "")[:40]))
    else:
        print("  Color temp. : no IES data. Confirm in the manufacturer datasheet")
    print("  Peak inten. : %.0f cd" % (peak or 0))
    if beam:
        print("  Beam angle  : ~%.0f°  (%s)" % (beam,
              "narrow spot" if beam < 20 else "medium" if beam < 45 else "wide/flood"))
    print("  Distribution: %s , %s" % (distribution, symmetry))
    print("  Dimensions  : %.2f × %.2f × %.2f %s" % (
        v["dimensions"][0], v["dimensions"][1], v["dimensions"][2],
        "m" if v["unit"] == 2 else "FEET (!)"))
    print("-" * 62)
    warnings = []
    if v["unit"] != 2:
        warnings.append("Units are FEET. ies2rad handles this, but ensure the scene uses METERS.")
    if v["non_ascii"]:
        warnings.append("%d non-ASCII bytes (e.g. ®). Edit the luminaire in "
                        "BINARY mode, otherwise it produces zero light." % v["non_ascii"])
    if not absolute and total_lumens and total_lumens < 1500:
        warnings.append("Low lumens (%d lm). One luminaire may be weak on a tall facade. "
                        "Consider more luminaires or boosting with ies2rad -m." % int(total_lumens))
    if v["photometry_type"] != 1:
        warnings.append("Photometry is not type C (type %d). Verify orientation in the render." % v["photometry_type"])
    if warnings:
        print("  WARNINGS:")
        for u in warnings:
            print("   ⚠ " + u)
    else:
        print("  No warnings. The file appears valid.")
    print("  Convert to Radiance:  ies2rad -m 1 -o <name> \"%s\"" % v["file"])
    print("=" * 62)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for path in sys.argv[1:]:
        try:
            print_analysis(path)
        except Exception as e:
            print("ERROR (%s): %s" % (path, e))
