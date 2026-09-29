# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Age-dependent glare: veiling luminance from evalglare sources (CIE 146, Stiles-Holladay).

    Lv = sum(10 * E_i / theta_i^2) * (1 + (age / 70)^4)
    E_i = L_i * omega_i * cos(theta_i)

The formula applies to angles greater than 1 and less than 30 degrees.
Sources outside this range are excluded from the sum
but are counted, whether they are luminaires or surfaces reflecting light.
These functions do not call Radiance. The command flow is in `core.__main__.glare`.
"""
import math
import re

LOWER_ANGLE, UPPER_ANGLE = 1.0, 30.0
GENERAL_FORMULA = 'Lv = sum(E * (10/theta^3 + (5/theta^2 + 0.1*p/theta)*(1+(age/62.5)^4) + 0.0025*p))'
AGES = (20, 40, 50, 60, 70, 80)
FORMULA = 'Lv = sum(10 * E / theta^2) * (1 + (age / 70)^4), E = L * omega * cos(theta)'
SYMBOLS = ('Symbols: Lv veiling luminance (cd/m²), E eye illuminance (lx), L luminance (cd/m²), omega solid angle (sr), '
           'theta angle between view and source direction (degrees), age in years')
GENERAL_SYMBOLS = ('Symbols: Lv veiling luminance (cd/m²), E eye illuminance (lx), theta angle between view and source direction (degrees), '
                   'age in years, p eye pigmentation factor (no unit, 0 to 1.2)')
SOURCE_COLUMNS = ('L_s', 'Omega_s', 'xdir', 'ydir', 'zdir')
REQUIRED_SUMMARY_FIELDS = ('dgp', 'ugr')
LIMITATIONS = (
    'Materials are assumed to be matte. Glossy surfaces do not produce specular reflections in this scene.',
    'For luminaires imported from EVO, the emitting aperture size is a placeholder. The luminaire luminance '
    'and the resulting DGP, UGR and veiling luminance are therefore unreliable.',
    'The formula applies for 1 < angle < 30 degrees. Sources outside this range are excluded from the sum.',
)


def age_factor(age):
    """CIE 146 age factor: 1 + (age / 70)^4."""
    if isinstance(age, bool) or not (isinstance(age, (int, float)) and 0 < age <= 120):
        raise ValueError('Age must be greater than 0 and at most 120')
    return 1 + (age / 70.0) ** 4


def _unit_vector(v):
    length = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(length) or length < 1e-12:
        raise ValueError('Direction vector is zero or invalid')
    return [x / length for x in v]


def parse_evalglare(text):
    """`evalglare -d` stdout -> (sources, numeric summary, raw summary).

    Columns are located by their names in the header. Truncated rows, a source list
    inconsistent with the header count, a missing summary or invalid numbers raise ValueError.
    """
    names, expected, lines, raw_summary = None, None, [], None
    for line in text.splitlines():
        chunk = line.split()
        if not chunk:
            continue
        if len(chunk) > 2 and chunk[0].isdigit() and chunk[1:3] == ['No', 'pixels']:
            if names is not None:
                raise ValueError('evalglare output contains two source headers')
            expected, names = int(chunk[0]), chunk[1:]
            continue
        if line.lstrip().startswith('dgp,') and ':' in line:
            label, value = line.split(':', 1)
            labels, values = [x.strip() for x in label.split(',')], value.split()
            if len(labels) != len(values):
                raise ValueError('evalglare summary row is truncated')
            raw_summary = dict(zip(labels, values))
            continue
        try:
            numbers = [float(x) for x in chunk]
        except ValueError:
            continue
        if names is None:
            raise ValueError('evalglare source row precedes the header')
        # Placeholder background values may be nonzero when the source count is zero.
        # A real source number in the No column, however, contradicts the header.
        if expected == 0:
            if numbers[0] != 0:
                raise ValueError('evalglare source count mismatch: header says 0, list contains a source')
            continue
        if len(numbers) != len(names):
            raise ValueError('evalglare source row is truncated: %d columns, expected %d'
                             % (len(numbers), len(names)))
        lines.append(numbers)
    if names is None:
        raise ValueError('evalglare source header is missing')
    missing = [name for name in ('No', *SOURCE_COLUMNS) if name not in names]
    if missing:
        raise ValueError('evalglare source column is missing: ' + ', '.join(missing))
    if expected != len(lines):
        raise ValueError('evalglare source count mismatch: %d in header, %d in list'
                         % (expected, len(lines)))
    if raw_summary is None:
        raise ValueError('evalglare summary row is missing')
    try:
        digest = {k: float(v) for k, v in raw_summary.items()}
    except ValueError as error:
        raise ValueError('evalglare summary value is not numeric') from error
    if any(k not in digest or not math.isfinite(digest[k]) for k in REQUIRED_SUMMARY_FIELDS):
        raise ValueError('evalglare DGP or UGR value is missing or invalid')
    if not 0 <= digest['dgp'] <= 1:
        raise ValueError('evalglare DGP value is not between 0 and 1')
    # Optional fields such as ugp can be inf in a dark view. Keep the raw text, store None as the value.
    digest = {k: v if math.isfinite(v) else None for k, v in digest.items()}
    # evalglare writes UGR -99 when the background luminance is 0. UGR divides by it, so it is undefined.
    if digest['ugr'] == -99 or digest.get('lum_backg') == 0:
        digest['ugr'] = None
    sources = []
    for numbers in lines:
        k = dict(zip(names, numbers))
        if not all(math.isfinite(x) for x in numbers) or k['L_s'] < 0 or k['Omega_s'] <= 0:
            raise ValueError('evalglare source %g: invalid luminance or solid angle' % k['No'])
        sources.append({'no': int(k['No']), 'luminance': k['L_s'], 'solid_angle': k['Omega_s'],
                          'direction': _unit_vector([k['xdir'], k['ydir'], k['zdir']])})
    return sources, digest, raw_summary


def veiling_luminance(sources, vd):
    """Add angle, eye illuminance and contribution to each source and return the sum before the age factor.

    The angle is between the view and source directions. An out-of-range source has
    a contribution of None and is excluded from the sum.
    """
    view = _unit_vector(vd)
    total = 0.0
    for k in sources:
        cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(view, _unit_vector(k['direction'])))))
        theta = math.degrees(math.acos(cosine))
        k['angle'] = theta
        k['illuminance'] = k['luminance'] * k['solid_angle'] * cosine
        # CIE 146 excludes the endpoints. Do not include them through acos rounding.
        k['in_range'] = LOWER_ANGLE + 1e-9 < theta < UPPER_ANGLE - 1e-9
        k['contribution'] = 10 * k['illuminance'] / theta ** 2 if k['in_range'] else None
        if k['in_range']:
            total += k['contribution']
    return total


def age_table(total, age):
    """Age factor and veiling luminance for ages 20, 40, 50, 60, 70, 80 and the requested age."""
    ages = sorted(set(AGES) | {age})
    return [{'age': y, 'multiplier': age_factor(y), 'veiling_luminance': total * age_factor(y)}
            for y in ages]


def check_eye_pigmentation(p):
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1.2:
        raise ValueError('Eye pigmentation factor must be a finite number between 0 and 1.2')
    return p


def general_sum(sources, age, p=0.5):
    """CIE general equation, with angle in degrees and nonnegative E in lux at the eye plane.

    NIST pub_id=917534 equation 4, NODD (Applied Optics 54, 1564), equation 1.
    Valid on the open interval 0.1 < angle < 100. Does not extend image coverage.
    """
    age_factor(age)
    check_eye_pigmentation(p)
    total = 0.0
    for k in sources:
        t, e = k['angle'], k['illuminance']
        if not math.isfinite(t) or not math.isfinite(e):
            raise ValueError('The general equation requires finite angle and illuminance')
        if 0.1 < t < 100:
            if e < 0:
                raise ValueError('The general equation does not allow negative eye illuminance')
            total += e * (10 / t**3 + (5 / t**2 + 0.1*p / t) * (1 + (age/62.5)**4) + 0.0025*p)
    return total


def object_names(rtrace_output, number, scene):
    """Map `rtrace -oms` rows to CTScene names.

    `faceN` is triangle N, named after its CTScene material. A name starting with `lN`
    or `lN.` is light N. A ray miss or an unresolved name raises
    ValueError rather than inventing a name.
    """
    lines = rtrace_output.splitlines()
    if len(lines) != number:
        raise ValueError('Incomplete source-to-object mapping: %d sources, %d rtrace rows'
                         % (number, len(lines)))
    mesh, lights = scene.get('mesh') or {}, scene.get('lights') or []
    result = []
    for i, line in enumerate(lines, 1):
        chunk = line.split('\t')
        radiance = chunk[1].strip() if len(chunk) > 1 else ''
        if radiance in ('', '*'):
            raise ValueError('No object found in the direction of source %d' % i)
        face, light = re.fullmatch(r'face(\d+)', radiance), re.match(r'l(\d+)(\.|$)', radiance)
        try:
            if face:
                n = int(face.group(1))
                material = mesh['materials'][mesh['m'][mesh['f'][3 * n]]]
                name = material.get('name')
            elif light:
                name = lights[int(light.group(1))].get('name')
            else:
                raise LookupError
        except (LookupError, TypeError):
            raise ValueError('Could not resolve the object for source %d in the scene: %s' % (i, radiance)) from None
        result.append({'object': name or radiance, 'radiance_name': radiance})
    return result


def _table(headers, lines):
    widths = [max(len(r[i]) for r in [headers, *lines]) for i in range(len(headers))]
    return ['  '.join(h.ljust(widths[i]) for i, h in enumerate(r)).rstrip()
            for r in [headers, *lines]]


def text(result):
    """GLARE.txt: summary, source table and age table."""
    def vector(v):
        return ' '.join('%g' % round(x, 4) for x in v)
    b, e = result['view'], result['evalglare']
    text = ['CTLux glare analysis', 'Scene: ' + result['scene'], '', 'SUMMARY',
            'Viewpoint (m): ' + vector(b['vp']), 'View direction (direction vector, no unit): ' + vector(b['vd']),
            'View origin: ' + b['source'],
            'Source count: %d' % result['source_count'],
            'Sources outside the range (1 < angle < 30 degrees): %d' % result['out_of_range_count'],
            'evalglare DGP (0 to 1, no unit): ' + e['raw']['dgp'], 'evalglare UGR (index, no unit): ' + (e['raw']['ugr'] if e.get('value', {}).get('ugr', 0) is not None else
                                'not defined, background luminance is 0 (evalglare wrote %s)' % e['raw']['ugr']),
            'Formula: ' + FORMULA, SYMBOLS, '', 'SOURCES']
    lines = [[str(k['rank']), k['object'], k['radiance_name'], '%.1f' % k['luminance'],
                 '%.6f' % k['solid_angle'], '%.2f' % k['angle'], '%.2f' % k['illuminance'],
                 '-' if k['contribution'] is None else '%.3f' % k['contribution'],
                 'yes' if k['in_range'] else 'no'] for k in result['sources']]
    if lines:
        text += _table(['No.', 'Object', 'Radiance name', 'L (cd/m²)', 'Solid angle (sr)', 'Angle (°)',
                        'E at eye (lx)', 'Contribution (cd/m²)', 'In range'], lines)
        text.append('Contribution is before the age factor: 10 * E / theta^2.')
    else:
        text.append('- No sources')
    if 'eye_pigmentation' in result:
        text += ['', 'General formula: ' + GENERAL_FORMULA, GENERAL_SYMBOLS,
                 'Eye pigmentation factor p: %g' % result['eye_pigmentation'],
                 'Sources outside the range (0.1 < angle < 100 degrees): %d' % result['general_out_of_range_count']]
    text += ['', 'AGE TABLE']
    headers = ['Age (years)', 'Factor (no unit)', 'Veiling luminance (cd/m²)']
    lines = [[str(y['age']) + (' (requested)' if y['age'] == result['age'] else ''),
                 '%.2f' % y['multiplier'], '%.3f' % y['veiling_luminance']] for y in result['age_table']]
    if 'eye_pigmentation' in result:
        headers.append('General veiling luminance (cd/m²)')
        for row, y in zip(lines, result['age_table']):
            row.append('%.3f' % y['general_veiling_luminance'])
    text += _table(headers, lines)
    if result.get('warnings'):
        text += ['', 'WARNINGS', *('- ' + item.rstrip('.') for item in result['warnings'])]
    if result.get('limitations'):
        text += ['', 'LIMITATIONS', *('- ' + item.rstrip('.') for item in result['limitations'])]
    return '\n'.join(text) + '\n'
