# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Headless command line: source -> CTScene -> Radiance conversion, products and glare commands."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import math
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import tempfile
import warnings

os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


SUPPORTED_FORMATS = {'.ls10', '.evo', '.obj', '.stl', '.3ds', '.usda', '.usdc', '.usdz', '.usd',
          '.ies', '.ldt', '.glb', '.gltf', '.dae', '.fbx'}
PRODUCT_FORMATS = {'.evo', '.zip', '.gldf', '.ldt', '.ies'}
# Appears directly after each report or product table heading when photometry files are written.
PHOTOMETRY_NOTICE = ['Photometric data belongs to its owner, normally the luminaire manufacturer.',
                  'These files were written solely to rebuild this project for calculation.']


class _Cancelled(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _prepare_output(input_path, output_directory, supported):
    """Validate the input and prepare a new or empty output directory using the same rule for all commands.

    When supported is None, input is a scene directory.
    """
    source = Path(input_path).resolve(strict=True)
    if supported is None:
        if not source.is_dir():
            raise ValueError('Not a scene directory: ' + source.name)
    elif not source.is_file() or source.suffix.lower() not in supported:
        raise ValueError('Unsupported input format: ' + source.suffix)
    output = Path(output_directory).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError('The output path must not contain symbolic links')
    # absolute() keeps '..' components. Normalise after the symlink check so nesting compares real locations.
    output = Path(os.path.normpath(os.path.abspath(output)))
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError('The output directory must be new or empty')
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('Input and output must not be nested')
    output.mkdir(exist_ok=True)
    return source, output


def _classify_notice(message, partial, skipped, limits):
    normalized = message.casefold()
    target = limits if 'limit' in normalized else skipped if any(
        word in normalized for word in ('skipped', 'not added', 'not applied', 'not transferred')) else partial
    if message not in target:
        target.append(message)


def _tool_warnings(entries):
    """Count files sharing the same tool/message. Preserve notifications without filenames."""
    groups, order = {}, []
    for entry in entries:
        for line in str(entry).splitlines():
            match = re.fullmatch(r'(ies2rad|xform): (.+?): (.+)', line)
            if not match:
                if line not in order:
                    order.append(line)
                continue
            tool, filename, message = match.groups()
            key = (tool, message)
            if key not in groups:
                groups[key] = []
                order.append(key)
            if filename not in groups[key]:
                groups[key].append(filename)
    return [('%s: %s, in %d files, first files: %s' %
             (item[0], item[1], len(groups[item]), ', '.join(groups[item][:3])))
            if isinstance(item, tuple) else item for item in order]


def _evalglare_notices(text):
    # evalglare 3.06 messages: try longer sentences before their shorter forms.
    small = '. DGP may underestimate glare sources'
    replacements = {
        'Low brightness scene. Vertical illuminance less than 380 lux! dgp might underestimate glare sources':
            'Low brightness scene. Vertical illuminance is below 380 lux' + small,
        'Low brightness scene. dgp below 0.2! dgp might underestimate glare sources':
            'Low brightness scene. DGP is below 0.2' + small,
        'Vertical illuminance is below 100 lux !!': 'Vertical illuminance is below 100 lux',
        'Vertical illuminance is below 100 lux': 'Vertical illuminance is below 100 lux',
        'Low brightness scene. dgp below 0.2': 'Low brightness scene. DGP is below 0.2',
        'Notice:': 'Notice:', 'Warning:': 'Warning:',
    }
    result = []
    for line in text.splitlines():
        if line.strip():
            translated = line.strip()
            for source, target in replacements.items():
                translated = re.sub(re.escape(source), lambda _: target, translated, flags=re.IGNORECASE)
            result.append('evalglare notification: ' + translated)
    return result


def _write_report(output, source, status, read, partial, skipped, limits, error=None, photometry=False):
    sections = [('READ', read), ('PARTIAL / ASSUMED', partial),
                ('SKIPPED', skipped), ('LIMITS', limits)]
    text = ['CTLux', *(PHOTOMETRY_NOTICE if photometry else []), 'Status: '+status, 'Input: '+source.name]
    for label, entries in sections:
        text += ['', label, *(['- '+str(x).rstrip().rstrip('.') for x in _tool_warnings(entries)] or ['- None'])]
    if error:
        text += ['', 'ERROR', str(error)]
    (output/'REPORT.txt').write_text('\n'.join(text)+'\n', encoding='utf-8')


def _roll_back(delivery):
    # Roll back only files delivered by this operation. Return the name and reason
    # for a file that cannot be removed, without raising over the original error.
    remaining = []
    for item in delivery:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
        except OSError as error:
            remaining.append('%s (%s)' % (item.name, error))
    return remaining


def _error_text(error, remaining):
    if not remaining:
        return error
    return '%s\nOutput file that could not be rolled back: %s' % (error, ', '.join(remaining))


def convert(input_path, output_directory, *, source_limit_mib=1024, triangle_limit=3_000_000):
    """Convert one file into a new or empty output directory without writing to the source.

    Return a dict for successful or partial results. Raise an exception if the source
    cannot be read or output cannot be written. To suppress bytecode, start the calling
    process with -B.
    """
    from core.scene_model import _limit
    source_limit_mib = _limit(source_limit_mib, 1024)
    triangle_limit = _limit(triangle_limit, 3_000_000)
    source, output = _prepare_output(input_path, output_directory, SUPPORTED_FORMATS)
    read, partial, skipped = [], [], []
    limits = ['CTScene limits: %d MiB total source data, %s triangles, 4096 vertices per face. Exceeding a limit is an error.'
              % (source_limit_mib, format(triangle_limit, ','))]
    delivery = []
    caught = []
    def add_notice(message):
        _classify_notice(message, partial, skipped, limits)
    def emit_report(status, error=None):
        for warning in caught:
            add_notice(str(warning.message))
        # Write the notice only when a photometry file has been delivered.
        _write_report(output, source, status, read, partial, skipped, limits, error,
                   error is None and any((output/'light').glob('*.ies')))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # Converter working files stay inside the output directory.
            with tempfile.TemporaryDirectory(prefix='.conversion-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as engine, format_detector as format_detector, scene_model as scene_model, scene_writers as scene_writers
                from core.tools.ies_analysis import read_ies
                from core.file_safety import internal_path
                for name in ('model', 'light', '_cache'):
                    (work/name).mkdir()
                local = work/('input'+source.suffix.lower())
                # Resolve OBJ sidecar files from the original directory in read-only mode.
                if source.suffix.lower() != '.obj':
                    shutil.copyfile(source, local)
                project = {'name': source.stem, 'geometry': [], 'luminaires': []}
                suffix = source.suffix.lower()
                root = work
                if suffix == '.obj':
                    root = source.parent
                    project['geometry'] = [{'file': source.name}]
                    partial.append('OBJ has no units. Coordinates were assumed to use meters and Z-up.')
                elif suffix in ('.ies', '.ldt'):
                    ies = work/'light/input.ies'
                    if suffix == '.ldt':
                        engine.ldt_to_ies(str(local), str(ies))
                        read.append('LDT symmetry was expanded and an absolute candela IES table was written.')
                    else:
                        shutil.copyfile(local, ies)
                    photo = read_ies(ies)
                    read.append('IES: %d vertical × %d horizontal = %d candela values' %
                                (photo['n_vertical'], photo['n_horizontal'], len(photo['candela'])))
                    project['luminaires'] = [{'name': source.stem, 'ies': 'light/input.ies',
                        'x': 0, 'y': 0, 'z': 0, 'type': 'point', 'direction': [0,0,-1], 'dimmer': 1}]
                    partial.append('Photometry input: position [0,0,0], direction [0,0,-1] and dimmer 1 were assumed. No geometry.')
                elif suffix == '.evo':
                    models, luminaires = format_detector._place_evo(str(local), str(work),
                        str(work/'model/input.obj'), read, partial)
                    project['geometry'] = [{'file': p} for p in models]
                    project['luminaires'] = luminaires
                    partial.append('EVO reading covers the STEP room/placement subset. Proprietary M3D/GDMS details and the full material model are unsupported.')
                elif suffix in engine.USD_EXTENSIONS:
                    # Resolve USD/glTF sidecar files relative to the original source in read-only mode.
                    geo, luminaires, note = engine.run_usd_bridge(str(source), str(work/'model/input.obj'))
                    if geo:
                        project['geometry'] = [{'file': 'model/input.obj'}]
                    project['luminaires'], notes = engine.usd_luminaires(luminaires)
                    partial.extend(notes)
                    if note:
                        partial.append(note)
                    partial.append('USD static mesh/light subset: animation and the full material network are not transferred.')
                else:
                    src = local if suffix == '.ls10' else source
                    engine.convert(str(src), str(work/'model/input.obj'))
                    project['geometry'] = [{'file': 'model/input.obj'}]
                    if suffix == '.ls10':
                        project['luminaires'] = engine.lumion_luminaires(str(local))
                        report = json.loads((work/'model/input.obj.import.json').read_text())
                        partial.extend(report['warnings'])
                        if report['skipped']:
                            skipped.append('Lumion geometry records: '+str(report['skipped']))
                    else:
                        partial.append('Assimp geometry bridge: units, axes and material semantics must be verified against the source application.')
                for luminaire in project['luminaires']:
                    if luminaire.get('product_rad') and not luminaire.get('ies'):
                        ies = Path(luminaire['product_rad']).with_suffix('.ies')
                        if (root/ies).is_file():
                            luminaire['ies'] = ies.as_posix()
                scene = scene_model.scene_from_project(str(root), project,
                    max_source_bytes=source_limit_mib * 1048576, max_triangles=triangle_limit)
                scene['camera'] = scene_writers.default_camera(scene)
                partial.append('The view was framed automatically. The source camera was not transferred.')
                def asset(ref):
                    return {'data': Path(internal_path(root, ref)).read_bytes()}
                result = scene_writers.export_radiance(scene, work/'result', asset, max_triangles=triangle_limit)
                for message in result['warnings']:
                    add_notice(message)
                read.append('%d vertices, %d triangles and %d lights were added to the CTScene and Radiance scene.' %
                            (result['stats']['vertices'], result['stats']['triangles'], result['stats']['lights']))
                if suffix == '.evo':
                    limits.append('EVO luminaire limit: 4000. Excess instances are also reported as a skipped count.')
                # Point IES references to delivered paths before deleting the temporary directory.
                for i, light in enumerate(scene['lights']):
                    delivered = work/'result/light'/('l%d.ies' % i)
                    if delivered.is_file():
                        ref = 'light/l%d.ies' % i
                        light['source']['ies'] = ref
                        light['source'].pop('product_rad', None)
                        light['original']['ies'] = ref
                        light['original'].pop('product_rad', None)
                for warning in caught:
                    add_notice(str(warning.message))
                scene['warnings'] = list(dict.fromkeys([*scene['warnings'], *partial, *skipped, *limits]))
                (work/'result/scene.ctlux.json').write_text(json.dumps(scene, ensure_ascii=False,
                    indent=2, allow_nan=False)+'\n', encoding='utf-8')
                for item in (work/'result').iterdir():
                    delivery.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        emit_report('partial' if partial or skipped or caught else 'success')
        return {'output': str(output), 'stats': result['stats'], 'report': 'REPORT.txt'}
    except (_Cancelled, KeyboardInterrupt):
        emit_report('cancelled', _error_text('Cancelled.', _roll_back(delivery)))
        raise
    except Exception as error:
        # If delivery fails partway through, roll back this run's delivered files and retain REPORT.txt.
        emit_report('error', _error_text(error, _roll_back(delivery)))
        raise


def _available_name(directory, base, extension):
    # Prevent a second product with the same name from overwriting the first: _2, _3.
    item_name, i = base + extension, 2
    while (directory/item_name).exists():
        item_name = '%s_%d%s' % (base, i, extension)
        i += 1
    return item_name


def _product_row(ies, data, item_name, manufacturer, lumens, usage):
    from core.tools.ies_analysis import beam_angle
    beam, _ = beam_angle(data['vertical_angles'], data['candela'], data['n_vertical'], data['n_horizontal'])
    return {'name': item_name, 'manufacturer': manufacturer, 'lumens': lumens,
            'beam_angle': round(beam, 1) if beam else None,
            'peak_candela': round(max(data['candela']) * data['multiplier'], 1),
            'c_planes': data['n_horizontal'], 'gamma_angles': data['n_vertical'],
            'usage': usage, 'ies': ies.name}


def _product_table(source, lines, rejected):
    headers = [('name', 'Name'), ('manufacturer', 'Manufacturer'), ('lumens', 'Lumens (lm)'), ('beam_angle', 'Beam angle (°)'),
                 ('peak_candela', 'Peak intensity (cd)'), ('c_planes', 'C planes'), ('gamma_angles', 'Gamma angles'),
                 ('usage', 'Usage'), ('ies', 'IES file')]
    def cell(value):
        return '-' if value is None else '%g' % value if isinstance(value, float) else str(value)
    table = [[label for _, label in headers]]
    table += [[cell(line[key]) for key, _ in headers] for line in lines]
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    text = ['CTLux product table', *PHOTOMETRY_NOTICE, 'Input: '+source.name, '']
    text += ['  '.join(h.ljust(widths[i]) for i, h in enumerate(r)).rstrip() for r in table]
    text += ['', 'REJECTED']
    text += ['- %s: %s' % (r['name'], str(r['reason']).rstrip().rstrip('.')) for r in rejected] or ['- None']
    return '\n'.join(text)+'\n'


def products(input_path, output_directory):
    """Extract lighting products from DIALux evo or photometry into a new or empty directory.

    Write one IES per product, PRODUCTS.txt, PRODUCTS.json and REPORT.txt without
    writing to the source. Do not invent values for products with missing lumens or invalid candela tables:
    skip their IES output, report the reason and continue with other products. Raise an
    exception if no IES file can be written.
    """
    source, output = _prepare_output(input_path, output_directory, PRODUCT_FORMATS)
    read, partial, skipped, limits = [], [], [], []
    delivery = []
    caught = []
    def emit_report(status, error=None):
        for warning in caught:
            _classify_notice(str(warning.message), partial, skipped, limits)
        # On error or cancellation, no product files remain and no notice is written.
        _write_report(output, source, status, read, partial, skipped, limits, error, error is None)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # Intermediate files stay inside the output directory.
            with tempfile.TemporaryDirectory(prefix='.products-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as engine, importer, photometry as photometry
                from core.tools.ies_analysis import read_ies
                for name in ('input', 'raw', 'conversion', 'result'):
                    (work/name).mkdir()
                # Keep the original name: GLDF and EVO filenames derive from the input name.
                local = work/'input'/source.name
                shutil.copyfile(source, local)
                suffix = source.suffix.lower()
                result = work/'result'
                lines, rejected = [], []
                def reject(item_name, reason, usage=None):
                    rejected.append({'name': item_name, 'reason': reason, 'usage': usage})
                    skipped.append('%s: %s, IES not written.' % (item_name, reason))
                if suffix == '.evo':
                    red = []
                    parse_records = importer.evo_ies(str(local), str(work/'raw'), rejected=red)
                    names = importer.write_evo_photometry(str(work/'raw'), str(result), project_name=source.stem)
                    file = {int(item_name.rsplit('_u', 1)[1][:-4]): item_name for item_name in names}
                    product = {}
                    for record in parse_records.values():
                        eq = int(record['product_representation_record'][1:])
                        product.setdefault(eq, dict(record, usage=0))['usage'] += 1
                    def product_name(record):
                        return record.get('product_name') or record.get('product_code') or  'product ' + record['product_representation_record']
                    for eq in sorted(product):
                        record = product[eq]
                        if eq not in file:
                            skipped.append('%s: IES could not be read. Skipped.' % product_name(record))
                            continue
                        ies = result/file[eq]
                        lines.append(_product_row(ies, read_ies(ies), product_name(record), None,
                                                     record['lumens'], record['usage']))
                    for record in red:
                        reject(product_name(record), record['reason'], record['usage'])
                    ents = engine._evo_step_ents(str(local)) or {}
                    total = sum(1 for record_type, _ in ents.values() if record_type == 'LuminaireElement')
                    linked = sum(k['usage'] for k in product.values()) + sum(k['usage'] for k in red)
                    read.append('EVO: %d luminaire instances and %d products read.' % (total, len(product) + len(red)))
                    if total > linked:
                        partial.append('%d EVO luminaire instances have no product photometry and were excluded from the product table.'
                                       % (total - linked))
                    limits.append('The manufacturer name is not read from EVO product records. The IES [MANUFAC] '
                                  'line states this.')
                else:
                    raw = work/'raw'
                    if suffix == '.zip':
                        names = importer.extract_photometry_zip(str(local), str(raw))
                    elif suffix == '.gldf':
                        names = [Path(p).name for p in importer.extract_gldf(str(local), str(raw))]
                    else:
                        shutil.copyfile(local, raw/source.name)
                        names = [source.name]
                    read.append('%d photometry files found.' % len(names))
                    seen = {}
                    for item_name in names:
                        info = {}
                        try:
                            if item_name.lower().endswith('.ldt'):
                                ies = work/'conversion'/(Path(item_name).stem + '.ies')
                                engine.ldt_to_ies(str(raw/item_name), str(ies), info)
                            else:
                                ies = raw/item_name
                            data = read_ies(ies)
                        except (ValueError, RuntimeError) as error:
                            reject(item_name, str(error))
                            continue
                        if info:
                            lumens = info['lumens']
                        elif data['lumens_per_lamp'] > 0:
                            lumens = data['lamp'] * data['lumens_per_lamp']
                        elif data['lumens_per_lamp'] == -1 and '_LUMENS' in data['keywords']:
                            try:
                                lumens = float(data['keywords']['_LUMENS'])
                                if not math.isfinite(lumens) or lumens <= 0:
                                    raise ValueError
                            except ValueError:
                                reject(item_name, 'invalid [_LUMENS]: positive, finite lumens required')
                                continue
                        else:
                            reject(item_name, 'no lumens (IES lumens field %g). Not calculated from the candela table'
                                   % data['lumens_per_lamp'])
                            continue
                        content = ies.read_bytes()
                        if content in seen:
                            partial.append('%s: same content as %s. Written once.' % (item_name, seen[content]))
                            continue
                        target = result/_available_name(result, ies.stem, '.ies')
                        target.write_bytes(photometry.annotated_ies(content))
                        seen[content] = target.name
                        key = data['keywords']
                        lines.append(_product_row(target, data,
                            key.get('LUMINAIRE') or key.get('LUMCAT') or Path(item_name).stem,
                            key.get('MANUFAC'), lumens, None))
                    limits.append('Absolute photometry IES files (lumens field -1) are treated as missing lumens unless they declare positive, finite [_LUMENS]. '
                                  'Lumens are not calculated from the candela table.')
                    limits.append('A photometry file alone is not a scene and has no usage count.')
                read.append('IES files written for %d products. %d products rejected.' % (len(lines), len(rejected)))
                limits.append('Beam angle is calculated at half peak intensity in the first C plane.')
                if suffix in ('.evo', '.zip'):
                    limits.append('The angle tag in the filename follows the current naming convention. '
                                  'It is twice the beam angle shown in the table.')
                if not lines:
                    raise RuntimeError('No lighting products can be written.')
                (result/'PRODUCTS.txt').write_text(_product_table(source, lines, rejected), encoding='utf-8')
                (result/'PRODUCTS.json').write_text(json.dumps({'input': source.name, 'products': lines,
                    'rejected': rejected}, ensure_ascii=False, indent=2, allow_nan=False)+'\n',
                    encoding='utf-8')
                for item in sorted(result.iterdir()):
                    delivery.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        emit_report('partial' if partial or skipped or caught else 'success')
        return {'output': str(output), 'products': len(lines), 'rejected': len(rejected),
                'report': 'REPORT.txt'}
    except (_Cancelled, KeyboardInterrupt):
        emit_report('cancelled', _error_text('Cancelled.', _roll_back(delivery)))
        raise
    except Exception as error:
        # If delivery fails partway through, roll back this run's delivered files and retain REPORT.txt.
        emit_report('error', _error_text(error, _roll_back(delivery)))
        raise


GLARE_RESOLUTION = {'draft': 400, 'medium': 800, 'final': 1200}


def _vector(value, item_name, nonzero=False):
    v = [float(x) for x in value]
    if len(v) != 3 or not all(math.isfinite(x) for x in v):
        raise ValueError(item_name + ' must contain three finite numbers')
    if nonzero and not any(v):
        raise ValueError(item_name + ' must not be zero')
    return v


def _viewpoint(scene_directory, vp, vd):
    """Complete a missing command-line view from view.vf."""
    if vp is not None and vd is not None:
        return vp, vd, 'command line'
    vf = scene_directory/'view.vf'
    if not vf.is_file():
        raise FileNotFoundError('The scene directory has no view.vf. Provide --vp and --vd')
    chunk = shlex.split(vf.read_text(encoding='utf-8'))
    file = {}
    for key in ('-vp', '-vd'):
        if key not in chunk:
            raise ValueError('view.vf does not contain %s' % key)
        i = chunk.index(key)
        file[key] = _vector(chunk[i+1:i+4], 'view.vf ' + key, key == '-vd')
    source = 'view.vf' if vp is None and vd is None else 'command line and view.vf'
    return vp or file['-vp'], vd or file['-vd'], source


def glare(scene_directory, output_directory, vp=None, vd=None, age=75, quality='medium', eye_pigmentation=0.5):
    """Analyze age adjusted glare from the viewpoint in a scene produced by `convert`.

    Render a fisheye image, find sources with evalglare and trace a ray from the eye
    toward each source to identify its surface. Calculate age adjusted veiling luminance
    from that source list. Write GLARE.txt, GLARE.json, eye.png, marked.png and
    REPORT.txt without writing to the scene directory. Raise an exception if evalglare
    or surface matching fails. Do not invent values.
    """
    from core import glare as analyze
    if quality not in GLARE_RESOLUTION:
        raise ValueError('Quality must be draft, medium or final')
    analyze.age_factor(age)
    analyze.check_eye_pigmentation(eye_pigmentation)
    vp = None if vp is None else _vector(vp, 'View point')
    vd = None if vd is None else _vector(vd, 'View direction', True)
    source, output = _prepare_output(scene_directory, output_directory, None)
    read, partial, skipped, limits = [], [], [], list(analyze.LIMITATIONS)
    if vp is None or vd is None:
        partial.append('The automatic view is for rendering. Glare analysis requires an eye position inside the room. '
                       'Provide both --vp and --vd.')
    limits.append('The surface name is the CTScene material name. CTScene does not store an object name per triangle.')
    limits.append('The general equation applies to 0.1 < angle < 100 degrees. The 180 degree fisheye image '
                  'does not cover directions more than 90 degrees from the view. The general total covers only detected sources.')
    read.append('Eye pigmentation factor p: %g.' % eye_pigmentation)
    limits.append('The scene has no sky or daylight. Only scene lights are included in the calculation.')
    delivery = []
    caught = []
    def emit_report(status, error=None):
        for warning in caught:
            _classify_notice(str(warning.message), partial, skipped, limits)
        _write_report(output, source, status, read, partial, skipped, limits, error)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # Octree, HDR and ray files stay inside the output directory.
            with tempfile.TemporaryDirectory(prefix='.glare-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as engine
                rad, scene_json = source/'scene.rad', source/'scene.ctlux.json'
                for file in (rad, scene_json):
                    if not file.is_file():
                        raise FileNotFoundError('The scene directory has no %s' % file.name)
                text = rad.read_text(encoding='utf-8')
                # Radiance accepts ! between primitives, including inline and after control characters.
                # Static conversion output needs no command escapes. Reject ! even in comments or strings
                # rather than guessing Radiance's token/comment boundaries. This is not a Radiance sandbox.
                if '!' in text:
                    raise ValueError('scene.rad contains an external command marker (!). Only static conversion output is accepted')
                scene = json.loads(scene_json.read_text(encoding='utf-8'))
                vp, vd, view_source = _viewpoint(source, vp, vd)
                read.append('View: %s, point (m) %s, direction %s.' % (view_source,
                            ' '.join('%g' % x for x in vp), ' '.join('%g' % x for x in vd)))
                work_dir = work/'scene'
                (work_dir/'_cache').mkdir(parents=True)
                # Stage exactly the validated snapshot, without reopening a mutable source file.
                (work_dir/'scene.rad').write_text(text, encoding='utf-8')
                light = source/'light'
                # A link or device under light/ could point outside the scene or never end when read.
                if light.is_symlink() or light.exists() and not light.is_dir() or any(p.is_symlink() or not (p.is_file() or p.is_dir())
                                             for p in (light.rglob('*') if light.is_dir() else ())):
                    raise ValueError('light/ must contain only regular files and folders')
                if light.is_dir():
                    shutil.copytree(light, work_dir/'light')
                engine.prepare_environment()
                code, _, se = engine.sh('oconv scene.rad > _cache/scene.oct', cwd=work_dir)
                if code:
                    raise RuntimeError('oconv failed: ' + se[-500:])
                direction = analyze._unit_vector(vd)
                up = [0, 0, 1] if abs(direction[2]) < 0.999999 else [0, 1, 0]
                view = '-vta -vp %s -vd %s -vu %s -vh 180 -vv 180' % tuple(
                    ' '.join('%.12g' % x for x in v) for v in (vp, vd, up))
                parse = GLARE_RESOLUTION[quality]
                engine.render(str(work_dir/'_cache/scene.oct'), view, quality, parse, parse, str(work/'eye.hdr'))
                read.append('Fisheye image: %d x %d pixels, quality %s.' % (parse, parse, quality))
                code, so, se = engine.sh('evalglare -d -c marked.hdr eye.hdr', cwd=work)
                partial.extend(_evalglare_notices(se))
                partial.extend(_evalglare_notices('\n'.join(
                    line for line in so.splitlines() if re.match(r'\s*(notice|warning)\b', line, re.I))))
                if code:
                    raise RuntimeError('evalglare failed (%d): %s' % (code, se[-500:]))
                sources, digest, raw_summary = analyze.parse_evalglare(so)
                if digest['ugr'] is None:
                    partial.append('evalglare UGR is not defined because the background luminance is 0. '
                                   'The written value %s is not a measurement.' % raw_summary['ugr'])
                names = []
                if sources:
                    (work/'rays.txt').write_text(''.join('%s\n' % ' '.join('%.12g' % x for x in (*vp, *k['direction']))
                                                            for k in sources), encoding='utf-8')
                    code, so, se = engine.sh('rtrace -h -ab 0 -oms _cache/scene.oct < ../rays.txt', cwd=work_dir)
                    if code:
                        raise RuntimeError('rtrace failed (%d): %s' % (code, se[-500:]))
                    names = analyze.object_names(so, len(sources), scene)
                for k, item_name in zip(sources, names):
                    k.update(item_name)
                total = analyze.veiling_luminance(sources, vd)
                sources.sort(key=lambda k: (not k['in_range'], -(k['contribution'] or 0), -k['illuminance']))
                for i, k in enumerate(sources, 1):
                    k['rank'] = i
                outside = sum(not k['in_range'] for k in sources)
                read.append('evalglare: %d sources, all matched to surface names.' % len(sources)
                            if sources else 'evalglare: no glare sources found.')
                if outside:
                    skipped.append('%d sources are outside 1 < angle < 30 degrees and were excluded from the veiling luminance total.'
                                   % outside)
                result = {'scene': source.name, 'age': age, 'quality': quality, 'resolution': parse,
                         'view': {'vp': vp, 'vd': vd, 'source': view_source},
                         'formula': analyze.FORMULA, 'validity_degrees': [analyze.LOWER_ANGLE, analyze.UPPER_ANGLE],
                         'evalglare': {'raw': raw_summary, 'value': digest},
                         'source_count': len(sources), 'out_of_range_count': outside,
                         'sources': [{a: k[a] for a in ('rank', 'no', 'object', 'radiance_name', 'luminance',
                                        'solid_angle', 'angle', 'illuminance', 'contribution', 'in_range', 'direction')}
                                       for k in sources],
                         'sum_before_age_factor': total,
                         'age_table': analyze.age_table(total, age)}
                result['eye_pigmentation'] = eye_pigmentation
                result['general_formula'] = analyze.GENERAL_FORMULA
                result['general_validity_degrees'] = [0.1, 100]
                result['general_out_of_range_count'] = sum(not 0.1 < k['angle'] < 100 for k in sources)
                for row in result['age_table']:
                    row['general_veiling_luminance'] = analyze.general_sum(sources, row['age'], eye_pigmentation)
                if result['general_out_of_range_count']:
                    skipped.append('%d sources are outside the general equation range of 0.1 < angle < 100 degrees.'
                                   % result['general_out_of_range_count'])
                delivery_directory = work/'result'
                delivery_directory.mkdir()
                engine.hdr_to_png(str(work/'eye.hdr'), str(delivery_directory/'eye.png'), automatic=True)
                engine.hdr_to_png(str(work/'marked.hdr'), str(delivery_directory/'marked.png'), automatic=True)
                result['warnings'] = list(dict.fromkeys([*partial, *skipped]))
                result['limitations'] = list(dict.fromkeys(limits))
                (delivery_directory/'GLARE.txt').write_text(analyze.text(result), encoding='utf-8')
                (delivery_directory/'GLARE.json').write_text(json.dumps(result, ensure_ascii=False, indent=2,
                    allow_nan=False)+'\n', encoding='utf-8')
                for item in sorted(delivery_directory.iterdir()):
                    delivery.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        emit_report('partial' if partial or skipped or caught else 'success')
        return {'output': str(output), 'sources': len(sources), 'out_of_range': outside,
                'report': 'REPORT.txt'}
    except (_Cancelled, KeyboardInterrupt):
        emit_report('cancelled', _error_text('Cancelled.', _roll_back(delivery)))
        raise
    except Exception as error:
        # If delivery fails partway through, roll back this run's delivered files and retain REPORT.txt.
        emit_report('error', _error_text(error, _roll_back(delivery)))
        raise


def _positive_integer(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('a positive integer is required')


def _quality_selection(value):
    aliases = {'draft': 'draft', 'medium': 'medium', 'final': 'final'}
    try:
        return aliases[value]
    except KeyError:
        raise argparse.ArgumentTypeError('quality must be draft, medium or final') from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    command = convert
    if argv and argv[0] == 'products':
        command, argv = products, argv[1:]
        parser = argparse.ArgumentParser(prog='python3 -m core products',
            description='Extract lighting products from DIALux evo or photometry into IES files and a product table.')
    elif argv and argv[0] == 'glare':
        command, argv = glare, argv[1:]
        parser = argparse.ArgumentParser(prog='python3 -m core glare',
            description='Find glare sources in a converted scene, identify surfaces and calculate age adjusted veiling luminance.')
    else:
        parser = argparse.ArgumentParser(prog='python3 -m core',
            description='Convert an input file to CTScene and Radiance. '
                        'Products: products <input> <output_directory>. '
                        'Glare: glare <scene_directory> <output_directory>.')
    parser.add_argument('input_path', metavar='scene_directory' if command is glare else 'input')
    parser.add_argument('output_directory', metavar='output_directory')
    option = {}
    if command is glare:
        parser.add_argument('--vp', nargs=3, type=float, metavar=('X', 'Y', 'Z'),
                            help='view point, read from view.vf when omitted')
        parser.add_argument('--vd', nargs=3, type=float, metavar=('DX', 'DY', 'DZ'),
                            help='view direction, read from view.vf when omitted')
        parser.add_argument('--eye-pigmentation', dest='eye_pigmentation',
                            type=float, default=0.5, metavar='P',
                            help='eye pigmentation factor from 0 to 1.2, default 0.5')
        parser.add_argument('--age', dest='age', type=int, default=75,
                            metavar='N', help='observer age, default 75')
        parser.add_argument('--quality', dest='quality', default='medium',
                            type=_quality_selection, metavar='{draft,medium,final}',
                            help='render quality and fisheye size, default medium')
    if command is convert:
        parser.add_argument('--source-limit-mib', dest='source_limit_mib',
                            type=_positive_integer, default=1024, metavar='N',
                            help='registered source data limit in MiB, default 1024')
        parser.add_argument('--triangle-limit', dest='triangle_limit',
                            type=_positive_integer, default=3_000_000, metavar='N',
                            help='triangle limit, default 3000000')
    args = parser.parse_args(argv)
    if command is convert:
        option = {'source_limit_mib': args.source_limit_mib, 'triangle_limit': args.triangle_limit}
    if command is glare:
        option = {'vp': args.vp, 'vd': args.vd, 'age': args.age, 'quality': args.quality, 'eye_pigmentation': args.eye_pigmentation}
    previous_state = {}
    def cancel(signum, frame):
        # Prevent a second signal from interrupting the first signal's child-process and temporary-file cleanup.
        for sig in previous_state:
            signal.signal(sig, signal.SIG_IGN)
        raise _Cancelled(signum)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_state[sig] = signal.getsignal(sig)
            signal.signal(sig, cancel)
        result = command(args.input_path, args.output_directory, **option)
    except (_Cancelled, KeyboardInterrupt) as error:
        print('Cancelled.', file=sys.stderr)
        return 128 + getattr(error, 'signum', signal.SIGINT)
    except Exception as error:
        print('Error: '+str(error), file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous_state.items():
            signal.signal(sig, handler)
    print('Output: '+result['output']+' / REPORT.txt')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
