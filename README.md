# CTLux: project files to Radiance scenes

[Turkish documentation and source](Turkish/README.md)

CTLux converts supported Lumion® 10 and DIALux evo project records into Radiance
scenes. This repository is its small, headless converter core with three commands.
Conversion recovers static geometry and lighting records, creates a neutral
CTScene JSON record, and writes a Radiance scene. `products` extracts the lighting
products of a DIALux evo file or a manufacturer photometry file into IES files and
a product table. `glare` analyses age-related glare in a converted
scene: it finds the glare sources seen from one viewpoint, labels each source by a representative ray hit, and
computes age-adjusted veiling luminance from the
scene. The commands use local files and local tools.

This independent project is not affiliated with, endorsed by or supported by
Act-3D B.V. or DIAL GmbH. Lumion® is a trademark of Act-3D B.V. and DIALux is a
trademark of DIAL GmbH. Product names identify the source applications
and their input file formats.

## Language and test data

The main `core/` and `tests/` trees use English names and explanations.
The [Turkish source and tests](Turkish/source/README.md) form a separate runnable copy.
Run each test suite from its own source root

Test geometry, luminaire identities and product labels are generated samples.
A small number of explicitly marked multilingual strings exercise Unicode and text parsing.
The synthetic products do not identify a manufacturer's measured luminaire.
Tests record expected behaviour so contributors can repeat the checks after changing the code

Start a fresh Python process for each language. Use only the selected source root
on `PYTHONPATH` or `sys.path`. Each `core` directory is a separate regular package.

## Requirements and use

The source uses Python 3.9+ standard library features, including
`Path.is_relative_to`. The test environment is Python 3.14.7 on Linux x86-64.
Compatibility with older interpreters has not been verified.
The conversion core has no mandatory third-party Python package.

- Radiance: `ies2rad` and `xform` for IES/LDT and recovered EVO photometry.
  `ies2rad` for EVO input to `products`.
  `oconv`, `rpict`, `evalglare`, `rtrace`, `pextrem`, `pcond`, `ra_ppm` for `glare`.
  `oconv`, `rpict`, `ra_ppm` for the rendering example below.
  The rendering API can also use `obj2mesh`, `obj2rad`,
  `rpiece`, `vwrays`, `pfilt`, `pcond`, `pextrem`, `falsecolor`, `getbbox`, and `ra_tiff`
- Optional Assimp executable: import of STL, 3DS, GLB, glTF, DAE and FBX
- Optional `usd-core` Python package: USD family input, in the same Python
  interpreter used to launch the command
- Optional `jsonschema`: independent JSON Schema test.
  The built-in CTScene validator always runs

Put executables on `PATH`. Alternatively set `RADIANCE_BIN` to the Radiance
`bin` directory for tools launched by the program itself. This does not configure
your shell: manual commands below (`oconv`, `rpict`, `ra_ppm`) require those tools
on `PATH`. For manual commands, set `RAYPATH` to `.` plus the Radiance `lib`
directory, separated by the platform's path separator. For its own child
processes the program prepends `.` and adds the distribution's `lib` directory
when found, preserving your `RAYPATH` and other entries. These tools are not
distributed here. If a settings file exists, it is read (never written) for
`radiance_bin` and `raypath`: `settings/settings.json` under `CTLUX_DATA_DIR`, or under
the source root (the directory that contains `core/`) when that variable is
not set. `RADIANCE_BIN` in
the environment takes precedence over the file's `radiance_bin`.

Run from this directory:

```sh
export PYTHONDONTWRITEBYTECODE=1
python3 -m core input.ls10 output
# Run without bytecode files:
python3 -B -m core input.evo output-evo
# Lighting products:
python3 -B -m core products input.evo products-output
# Glare for an older observer, on a conversion result:
python3 -B -m core glare output glare-output --vp 1 2 1.2 --vd 1 0 0.3 --age 75
```

The output directory must be new or empty, its parent must exist, and its path must contain no symlinks. Input
files remain unchanged. Temporary conversion files live inside the output and
are removed on completion or error. There is no project shelf, server, or UI.
Python's `-m` loader can cache the entry module *before* its code starts: use
`-B` or the environment setting above to prevent that write too.

The conversion result contains `scene.rad`, `geometry.rad`, `materials.rad`, `lights.rad`,
`view.vf`, `scene.ctlux.json`, and `REPORT.txt`. Photometric scenes also contain
`light/` with source IES and Radiance distribution files. `scene.rad` already
combines the three component RAD files. Do not load them twice.
Run Radiance with the output directory as its working directory, so distribution
file references remain valid when the entire output is moved.

`REPORT.txt` lists what was read, what remains partial or uses defaults, what was
skipped, and which limits apply. Reports use English labels. Exit 0 means conversion
produced a usable result, which may be partial. A conversion report always shows
`Status: partial`, because the view is framed automatically and the source
camera is not transferred. Unsupported or unreadable input
returns nonzero with a readable error. Conversion errors also leave a failure
report when the output directory could safely be created.
Ctrl+C prints a short cancellation message and exits 130. SIGTERM exits 143.
Both stop owned child processes and remove temporary and partly delivered files.
An already created output directory retains an explicit cancellation report.
If moving the results into the output fails part way, the files this run has
already delivered are removed as well. `REPORT.txt` keeps the error and the exit
code is 1. This holds for all three commands.


Conversion accepts positive integers for `--source-limit-mib N` and
`--triangle-limit N`. The defaults are 1024 MiB and 3,000,000 triangles. Example:
`python3 -B -m core input.ls10 output --source-limit-mib 2048 --triangle-limit 6000000`.
Large scenes need substantial memory and disk space. The source quota counts
registered OBJ/RAD, MTL and texture files in the CTScene builder, not compressed
input size or process RAM. The triangle quota is passed through to final
Radiance validation. Quota errors include the observed amount and the option
to raise it. `REPORT.txt` records the selected limits. No sampling is performed.
Identical tool warnings are grouped by message with a file count and the first
three filenames. Different messages remain separate.

## Lighting products

The command lists and writes the photometry used in a project at hand, to rebuild
that project in Radiance. It takes one input file. A folder or a second input is
rejected.

`python3 -B -m core products <input> <output_directory>`

Input: DIALux `.evo`, a manufacturer photometry package `.zip`, `.gldf`, `.ldt`, or `.ies`.
The output directory receives:

- One IES file per product
- `PRODUCTS.txt`: a readable table with name, manufacturer, lumens, beam angle,
  peak candela, number of C planes, number of gamma angles, number of uses in
  the scene and IES file name, followed by rejected products and their reasons
- `PRODUCTS.json`: the same information for programs, with `input`, `products`
  and `rejected` fields.
  Units: `lumens` in lm, `beam_angle` in degrees (twice the first C-plane angle
  from the axis to half-peak intensity, see Limits) and `peak_candela` in cd.
- `REPORT.txt`: the same four sections as conversion

File names: EVO products are named `project_<input>_<type>_<angle>d_<lumens>lm_u<record>.ies`.
`<input>` comes from the input file name (lower case, at most 16 characters). IES
members of a ZIP keep their name plus a `_<angle>d_<lumens>lm` tag when the beam
or lumens can be read. LDT members of
a ZIP keep their name with the `.ies` extension. GLDF members, also those of a
GLDF inside a ZIP, become `<gldf name>_<member name>.ies`. A single IES keeps its
name. A single LDT keeps its name with the `.ies` extension. A name that is
already taken receives `_2`, `_3`. Extraction never overwrites an existing file,
so members with the same name in different folders or GLDF files stay separate.

Every IES file written, here and in `light/` of a conversion result, carries this
notice before the `TILT=` line, in ASCII, as the LM-63-2002 user keyword `[_NOTICE]`:

```text
[_NOTICE] Photometric data belongs to its owner, normally the luminaire
[MORE] manufacturer. Written from a lighting project file to rebuild
[MORE] that project.
```

For a file from a package or a single file (ZIP, GLDF, IES, LDT) the second
sentence is `Copied from a file supplied by the user.` Keywords already in the
source file are kept. IES files written from EVO carry the four keywords that
LM-63-2002 requires with neutral values: `[TEST]`, `[TESTLAB]` and `[ISSUEDATE]`
are `not available` and `[MANUFAC]` is `not read from the project record`.
`PRODUCTS.txt` and `REPORT.txt` begin, right after the title, with a notice that
photometric data belongs to its owner, normally the luminaire manufacturer,
and that the files were written to rebuild the project for calculation. The conversion report
carries it only when photometry files were delivered. A failure or cancellation
report never carries it.

Rules:

- Missing lumens are never invented.
  Missing lumens cause rejection with a recorded reason and no IES output.
  An IES with absolute photometry (lumens field -1) is accepted only when its
  `[_LUMENS]` keyword declares a positive, finite total flux in lumens.
  Lumens are never computed from the candela table.
  LDT conversion preserves absolute candela and writes the source lamp flux to
  `[_LUMENS]`, so the resulting IES can be passed to `products` again.
- Generated EVO and LDT headers use ASCII text and include `[TEST]`, `[TESTLAB]`,
  `[ISSUEDATE]` and `[MANUFAC]`.
  LDT manufacturer and date come from the source, with empty values replaced by
  `not available` and names reduced to ASCII.
  Unknown CCT is omitted.
  `[_SOURCE]` identifies the EULUMDAT conversion separately from the
  adjacent `[_NOTICE]` and `[MORE]` ownership lines.
- A product with a negative, non-finite, or incomplete candela table is rejected.
  The reason is written to `REPORT.txt` and `PRODUCTS.txt`.
  Other products continue.
- Each product is written once, with EVO uses counted from the scene.
  IES files in a package are compared by their bytes after reading.
  Identical content is written once and the duplicate is reported.
- If no IES can be written, the exit code is 1 and only `REPORT.txt` remains
- Output directory rules, input immutability, and Ctrl+C/SIGTERM behaviour are
  the same as for conversion

Limits:

- The table's beam angle is twice the angle from the axis where intensity first
  falls to half of the peak in the first C plane.
  The angle tag in file names follows the current naming rule and doubles the
  table value again.
- EVO product records are not read for a manufacturer name.
  The `[MANUFAC]` line in IES files written from EVO says so.
  The LDT manufacturer field is preserved when present.
- A photometry file alone is not a scene, so its use count stays empty

Photometric data belongs to its owner, normally the luminaire manufacturer. This
repository contains no product files.

## Age related glare

`python3 -B -m core glare <scene_directory> <output_directory> [--vp X Y Z] [--vd DX DY DZ] [--age N] [--quality draft|medium|final] [--eye-pigmentation P]`

The scene directory is a conversion result: `scene.rad`, `scene.ctlux.json`,
and `view.vf` when `--vp` or `--vd` is missing. A missing value is taken
from `view.vf`. `--age` is the observer age, an integer from 1 to 120,
default 75. `--quality` selects
the Radiance quality and the square fisheye size: `draft` 400, `medium` 800
(default), `final` 1200 pixels. The up vector is +Z (+Y when looking straight
up or down). The scene directory is only read. The octree and HDR images live
in a temporary directory inside the output and are removed. Run time grows
with the number of lights and with quality: in a scene with hundreds of lights
even `draft` quality can take several minutes.

On success, the output directory receives exactly these files:

- `GLARE.txt`: a summary, source table and age table.
  The summary gives the viewpoint, direction, number of sources, number of
  sources outside the valid range, and DGP and UGR values from evalglare.
  When the background luminance is 0, evalglare writes UGR as -99. UGR is not
  defined then, so it is shown as not defined with the raw text. GLARE.json stores
  null and the report status is partial. Other optional evalglare summary fields
  that are not finite, such as ugp, are also stored as null.
  The source table gives rank, object name, Radiance identifier, luminance, solid
  angle, angle in degrees, eye illuminance, veiling contribution and range status,
  sorted by contribution in descending order.
  The age table gives factors and veiling luminance (cd/m²) for ages 20, 40, 50,
  60, 70, 80 and `--age`.
- `GLARE.json`: the same data for programs.
  Units: `vp` in metres, `vd` is a direction vector without unit (only its
  direction matters, it is not normalized), `luminance` in cd/m², `solid_angle`
  in sr, `angle` in degrees, `illuminance` in lx, `contribution`,
  `veiling_luminance` and `general_veiling_luminance` in cd/m². DGP is a value
  from 0 to 1 and UGR is an index, both without unit.
- `eye.png`: the tone mapped fisheye image
- `marked.png`: evalglare's check image with the sources marked
- `REPORT.txt`: the same sections as conversion

Formula (CIE 146, Stiles and Holladay):

```text
Lv  = sum(10 * E_i / theta_i^2) * (1 + (age / 70)^4)
E_i = L_i * omega_i * cos(theta_i)
```

`L_i` (cd/m²) and `omega_i` (sr) come from the evalglare source line. `theta_i`
is the angle in degrees between the view direction and the source direction.
The formula is valid for 1 < theta < 30 degrees, as stated by
[CIE 146:2002](https://www.cie.co.at/publications/cie-collection-glare-2002). A source outside that range
does not enter the sum. The number of such sources is written to `GLARE.txt`
and `REPORT.txt`. Age factors: 20: 1.01, 40: 1.11, 50: 1.26, 60: 1.54,
70: 2.00, 80: 2.71. The contribution column is shown before the age factor.

Every evalglare source counts, whether it is a luminaire or a surface that
reflects light, so reflected light is included by itself. For each source a
ray is traced from the eye in the source direction (`rtrace -oms`). The first surface hit by that ray supplies a representative label. This
does not identify every pixel of a broad source. A light (`lN`) uses its CTScene
light name and a triangle (`faceN`) uses its CTScene material name.

No invented values: if evalglare fails, if its output is cut or inconsistent
(a source line with the wrong number of columns, a source count that differs
from the header, missing DGP or UGR), or if a source cannot be mapped to an
object, the command exits 1 and only `REPORT.txt` remains. A view without glare
sources gives zero sources and zero veiling luminance. evalglare notices go to
the partial section of `REPORT.txt`. A `scene.rad` that contains `!` anywhere,
including comments and strings, is rejected, because `!` marks a shell command
in Radiance. Symbolic links and special files such as devices or named pipes
inside `light/` are also rejected. Output directory rules, input immutability and
Ctrl+C/SIGTERM behaviour are the same as for conversion. The render queue lock
below applies.

`REPORT.txt` always lists these three limits:

- Materials are assumed matte.
  Specular reflection from a glossy surface does not occur in this scene.
- For luminaires from EVO the size of the light emitting opening is a placeholder.
  The luminaire's own luminance and the DGP, UGR and veiling luminance derived
  from it are therefore not reliable.
- The formula accepts only sources with 1 < angle < 30 degrees

It also states that the object name of a surface is its material name and that
the scene has no sky or daylight. An IES light with zero luminous dimensions
becomes a 0.5 mm sphere through `ies2rad`. In the synthetic IES scene viewed
from 2 m it was not found as a source.


The automatic `view.vf` is intended for drawing. Choose an eye position
inside the room for glare analysis. The report reminds you when either `--vp`
or `--vd` is omitted. Notices from both evalglare stdout and stderr enter the
partial section. Low vertical illuminance and low DGP notices are explained
in English, and unrecognized messages are retained. With a zero source count in the
header, placeholder background values are ignored as sources, summary
validation still runs, and both sums are zero.

A second sum uses the CIE General Disability Glare equation:

```text
Lv_general = sum(E * (10/theta^3 + (5/theta^2 + 0.1*p/theta)*(1+(age/62.5)^4) + 0.0025*p))
0.1 < theta < 100 degrees
```

`--eye-pigmentation p` accepts a finite pigmentation factor from 0 to 1.2: black 0,
brown 0.5, light 1.0, very light 1.2. The default 0.5 is the middle example
in the NODD study, not a universal population average. The coefficients were
checked against the [NIST paper, equation 4](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=917534)
and [NODD, equation 1 and Table 11](https://doi.org/10.1364/AO.54.001564).
The angular domain agrees with the [CIE description](https://www.cie.co.at/publications/cie-collection-glare-2002).
The age table shows the original and general sums in separate columns.
The value p and the number of sources outside each domain are also reported.

The general sum covers detected sources only. The 180 degree fisheye does
not sample sources beyond 90 degrees from the viewing direction. The equation's
100 degree limit does not expand the camera field. Eye illuminance is
`L * omega * cos(theta)`. Negative illuminance is rejected by the general
formula. The 1 to 30 degree sum can exclude many ceiling luminaires
in real rooms. A zero sum alone does not establish absence of glare.

## Execution logic

`input → format reader → geometry/lights + warnings → CTScene validation → Radiance writer → report`

The command starts with empty geometry, light and warning lists. Each reader
adds records or reports a rejected record. The scene builder traverses faces,
checks indices and source size, and counts emitted triangles. It stops at the
end of the input or fails on a hard quota. It does not silently sample geometry.
The writer emits one polygon per triangle and one representation per enabled
light. IES distributions pass through real `ies2rad` and `xform`. Lights without
photometry retain explicitly approximate strength. The final file set is moved
from the temporary directory into the output. `REPORT.txt` and CTScene warnings
carry the observed losses. No application window or background service is started.

`products` flow: `input → photometry reader → per product validation → IES + table → report`.
For EVO, products are read from STEP records. For package, GLDF, LDT, and IES
input, each photometry file is one product. An invalid product is reported
separately and the flow continues.

`glare` flow: `scene directory → oconv → 180° fisheye (rpict) → evalglare -d → rtrace per source → CTScene name → formula → files → report`.

The glare calculation starts with a list of N detected sources and a zero
accumulator. Each iteration reads one source direction, luminance and solid
angle, then writes its viewing angle and eye illuminance. The original formula
adds a contribution only when 1 < angle < 30 degrees. After each iteration, the
accumulator equals the sum of accepted contributions among the processed sources.
The loop ends when all N sources have been processed.

The age table contains six fixed ages plus the requested age, with duplicates
removed. Each row applies its age factor to the original sum and starts a new
zero accumulator for the general formula. That accumulator traverses the same N
sources, adding contributions only for 0.1 < angle < 100 degrees. Its invariant
is again the sum of accepted contributions processed so far.

Angle and eye illuminance are calculated N times. The original formula makes N
range checks, and each age row makes N general range checks. Each accumulator
performs one addition per accepted source. These are source-level operation
counts, and do not include rendering, ray tracing or sorting. With N equal to
zero, no source iteration runs and both reported sums remain zero. The source
rows and age rows in `GLARE.txt` and `GLARE.json` expose these calculated values.

## Relationship to Radiance tools

- The standalone converter calls `ies2rad` for photometry and `xform` for light placement
- It writes scene polygons itself and does not run `obj2rad`, `obj2mesh`, `oconv` or `rpict` in this command
- `products` calls `ies2rad` for EVO photometry, while other inputs need no Radiance tool
- The rendering API uses `obj2mesh` for mesh caches and `obj2rad` for polygon/fallback geometry
- `oconv` creates octrees, `rpict` renders images, `rpiece` splits renders and `vwrays` checks native image dimensions
- `compile_scene` takes the sky description from the caller. The core does not generate sky text
- `xform` places geometry and lights
- `getbbox` finds bounds for camera framing and `ra_tiff` converts textures to Radiance pictures
- `pfilt` applies manual exposure, `pcond` automatic tone mapping, and `ra_ppm` produces pixels for PNG output
- `pextrem` reads extrema (including black-image detection) and `falsecolor` produces labelled analysis colours
- `glare` runs `oconv`, renders through the rendering API, calls `evalglare -d -c` for sources, DGP and UGR, and `rtrace -oms` for object names
- `pvalue` is used by tests only. Conversion and `products` do not call `rtrace`

## Credits and references

This program stands on the work of others. It calls their tools and uses
their published equations.

- Radiance: written by Greg Ward, developed at Lawrence Berkeley National
  Laboratory, tested with version 6.0.2.
  Ward, G. J. (1994), The RADIANCE Lighting Simulation and Rendering System,
  SIGGRAPH '94
- evalglare: written by Jan Wienold, distributed with Radiance, tested with
  version 3.06.
  DGP: Wienold, J. and Christoffersen, J. (2006), Evaluation methods and
  development of a new glare prediction model for daylight environments with
  the use of CCD cameras, Energy and Buildings, 38(7)
- Disability glare: CIE 146:2002, CIE Equations for Disability Glare.
  Vos, J. J. (2003), On the cause of disability glare and its dependence on
  glare angle, age and ocular pigmentation, Clinical and Experimental
  Optometry, 86(6)
- The general equation was checked against Uchida and Ohno (NIST, equation 4)
  and Williamson and McLin (2015), Nominal ocular dazzle distance (NODD),
  Applied Optics, 54(7), with links in the glare section above
- UGR: CIE 117-1995, Discomfort Glare in Interior Lighting
- Photometry file format: ANSI/IESNA LM-63-02
- Earlier public work on the two file formats is credited in the method
  notes, in the `RELATED_WORK.md` file of each method repository:
  [ctlux-ls10](https://github.com/JamesAugustus/ctlux-ls10) and
  [ctlux-evo](https://github.com/JamesAugustus/ctlux-evo)

## Input evidence

The author reports manual testing on macOS. The full Lumion/LS10 visual test
was performed on Linux. Lumion light recovery was partial, and the macOS trials
did not cover the full Lumion visual workflow. These manual observations are
separate from the automated Linux test results below

DAE and FBX are input formats only. DAE, FBX and GLB are imported through
Assimp. The export API writes GLB, OBJ, USDA and Radiance. GLB supports both
import and export. DAE and FBX export is not implemented.

“Works” means the named synthetic case passed, not complete support for all files
or versions. All entries below have a test or a stated evidence gap.

| Input | Status and scope | Evidence in `tests/` |
| --- | --- | --- |
| Lumion `.ls10` | Partial: mesh + lights, material, texture and version semantics limited | `test_core.CommandTest.test_cli_ls10`, `test_ls10_preservation`, `test_ls10_surface` |
| DIALux `.evo` | Partial: STEP rooms/layout, room faces point into the room, recoverable photometry, proprietary geometry incomplete | `test_core.CommandTest.test_cli_evo`, `test_evo_components`, `test_evo_identity`, `test_evo_photometry_validation` |
| OBJ | Works for tested static geometry, assumes metres/Z-up | `test_core.CommandTest.test_cli_obj`, `test_scene_model` |
| STL ASCII/binary, 3DS | Works for synthetic triangles through Assimp, no full material fidelity claim | `test_core.CommandTest.test_cli_assimp_formats` |
| GLB, glTF, DAE, FBX ASCII/binary | Works for synthetic static triangles through Assimp, animation/lights/full material networks not recovered | `test_core.CommandTest.test_cli_assimp_formats`, `test_assimp_bridge` |
| USDA | Works for tested static mesh/light subset with usd-core | `test_core.CommandTest.test_cli_usda`, `test_usd_native` |
| USDC, USDZ | Reader tested, full CLI path not separately measured | `test_usd_native.USDTest.test_usdc_binary`, `test_usd_native.USDTest.test_usdz_package` |
| IES | Works for tested angular tables, incomplete/extra values rejected | `test_core.CommandTest.test_cli_ies_ldt_use_real_photometry`, `test_core.LimitTest.test_ies_incomplete_and_extra_candela_are_errors` |
| LDT | Works for supported symmetry and one lamp set, unsupported tilt/symmetry rejected | `test_ldt_translation`, `test_core.CommandTest.test_cli_ies_ldt_use_real_photometry` |
| `products`: EVO, photometry ZIP, GLDF, LDT, IES | Synthetic products, missing lumens, invalid candela and duplicates are reported, same named members stay separate, ownership notice, neutral EVO header, unchanged `ies2rad` light, one input only | `test_core.ProductsTest` |
| `glare`: conversion result | Synthetic room with one ceiling light, formula, age factors, range count, evalglare parsing, object names, failures and cancellation | `test_glare` |
| Other Lumion versions, LSF, L3D | No reader | None |
| 3DM, IFC, ABC, PLY, X3D, LWO, OFF, MS3D, BLEND, AC | No reader | None |

## Limits

- EVO reads at most 4000 placed luminaires by default, reporting read, retained
  and skipped counts when the limit is reached.
  Unplaced records, cut STEP records and failed embedded FBX parts are reported.
  Furniture boxes have a separate 4000 limit with truncation warnings.
  The STEP reader supports a lexical subset of the format and is not a complete
  STEP validator
- Missing/mismatched Lumion light fields keep the original defaults: type 0,
  cone 1.0, width/height 1.0, white.
  Reports identify each light and field.
  Colour and intensity are not physically calibrated
- By default CTScene checks 1024 MiB total registered source data, 3,000,000 triangles,
  and 4096 vertices per face.
  These limits apply to scene building, with archive decompression and total
  input memory outside their scope
- Radiance materials use diffuse base colour.
  Textures, glass/metal shader semantics and smooth vertex normals are reported
  as unapplied.
  Original mesh attributes may remain in CTScene, and material file references
  are source metadata that can refer to textures outside the output.
  They are not a promise that textures were bundled
- IES/LDT alone produces a scene containing only a light at the origin facing -Z.
  No room is invented.
  Geometry without lights has no generated sky.
  The view is automatic
- An IES light is turned so that its nadir faces the light direction and its C0
  plane faces world +X projected perpendicular to that direction, or world +Y for
  a light horizontal along X. A downward light keeps C0 on +X as `ies2rad` writes
  it. Rotation about the light axis from the source file is not used, so an
  asymmetric distribution can face the wrong way
- Relative file names inside a project may not start with a drive prefix such as
  `a:`, on any platform, so a file named `a:b.txt` is rejected. Other names with a
  colon, such as `scan 12:30.obj`, are accepted outside Windows
- Full native round trips, all application versions, physical calibration,
  hostile-archive resource containment, and cross-platform native runs were not
  established by these synthetic tests

## Future work

These items are open. Contributions with synthetic test data are welcome

- EVO luminaire rotation about the light axis. The C0 plane now follows a fixed
  rule (see Limits). The EVO element frame's x axis is a likely source for the C0
  direction, but this needs a check against DIALux results for an asymmetric
  luminaire rotated in plan. `engine.photometry_rotation` already accepts a C0
  axis. The importer and the scene model would need to carry it. See the method
  notes of ctlux-evo, section 4 and Limits
- `falsecolor` with a `TMPDIR` that contains spaces. A shell safe `/tmp` or
  `/var/tmp` is used when one exists. Systems without either, such as Windows,
  are not covered
- Beam angle naming. The product table stores twice the first C-plane half-peak
  angle. The file name tag and the spot, downlight and wide classification double
  that table value again. These values should be reconciled with the usual
  full-cone meaning of beam angle
- Uplight tables whose vertical angles start at 90 degrees give a beam angle of 180
- When one EVO equipment record holds several `LampTypeChannel` records, the last
  value is used. Whether they should be summed is open
- Native runs on macOS and Windows
- Visual balance between the left and right halves of the view. The author
  observed that a view with a dark left side, a bright right side and a bright
  source at eye level on the right can be uncomfortable. Whether such an
  asymmetry is linked to visual discomfort or migraine is a research question.
  It is being studied in theory only. The core does not compute it yet

## Public calls for a future interface

The preferred standalone calls are
`core.__main__.convert(input_path, output_directory)`,
`core.__main__.products(input_path, output_directory)` and
`core.__main__.glare(scene_directory, output_directory, vp=None, vd=None, age=75, quality='medium', eye_pigmentation=0.5)`.

| Purpose | Names |
| --- | --- |
| Format identification | `core.format_detector.analyze`, `warnings` |
| Conversion | `core.importer.convert`, `lumion_luminaires`, `evo_luminaires`, `evo_rooms`, `run_usd_bridge` |
| Lighting products | `core.importer.evo_ies`, `write_evo_photometry`, `extract_photometry_zip`, `extract_gldf`, `ldt_to_ies` (`core.engine.ldt_to_ies` is the same function), `core.photometry.ies_source`, `core.tools.ies_analysis.read_ies`, `beam_angle` |
| Neutral scene | `core.scene_model.scene_from_project`, `validate_scene`, `core.evo_identity.enrich` |
| Writing | `core.scene_writers.export_scene` (`radiance`/`rad`, `glb`, `obj`, `usda`), `export_radiance`, `default_camera` |
| Camera | `core.scene_camera.camera_normalize`, `radiance_view`, `core.engine.auto_frame` |
| Radiance rendering | `core.engine.prepare_environment`, `compile_scene`, `view`, `free_view`, `render`, `hdr_to_png`, `falsecolor_png`, `pextrem` |
| Progress | `core.render_progress.workflow`, `workflow_status`, `status` |
| Glare | `core.glare.parse_evalglare`, `veiling_luminance`, `age_factor`, `age_table`, `general_sum`, `check_eye_pigmentation`, `object_names`, `text` |

`export_radiance` accepts `asset_resolver(relative_ies_path) -> {'data': bytes}`
for source photometry. `compile_scene(project_dir, project, sky_text)` takes the sky
text from the caller. `workflow_status(operation_id)` only reads work running inside
`workflow(operation_id)`. Passing a list instance as
`evo_ies(evo_path, project_dir, rejected=rejected_products)` records invalid
products in that list and continues. With the default `rejected=None`, an
invalid product raises an exception.

The caller chooses project and product output folders. Product functions take
`target_directory` explicitly: `extract_photometry_zip(source, target_directory)`,
`extract_gldf(source, target_directory)` and
`write_evo_photometry(project_dir, target_directory, project_name=None)`.
The core has no implicit library, inbox or project root. The rendering API writes
under `_cache` in the caller-provided project folder. `auto_frame` computes a
target when the project has none and writes `project.json` by default.
`auto_frame(project_dir, project, save=False)` prevents that write. The
render queue lock is the file `/tmp/ctlux-render-<user id>.lock` (change it with
`CTLUX_RENDER_LOCK`). `CTLUX_RENDER_WORKERS=1` forces serial rendering. Importing the modules creates no folder and imports no interface.

## Synthetic examples and rendering

```sh
python3 -B tests/synthetic.py /tmp/ctlux-inputs
python3 -B -m core /tmp/ctlux-inputs/triangle.obj /tmp/ctlux-output
cd /tmp/ctlux-output
oconv scene.rad > scene.oct
rpict -vf view.vf -x 320 -y 240 -ab 0 -ad 64 -as 16 -av .2 .2 .2 scene.oct > draft.hdr
ra_ppm -g 2.2 draft.hdr > draft.ppm
```

Here `-av` supplies a draft viewing ambient term for an otherwise unlit triangle.
The resulting image is a geometry preview. `engine.hdr_to_png` provides a PNG conversion
API. In this version a synthetic floor with an IES light was compiled and
rendered at 64×43 pixels through `engine`. The image was not empty.

Synthetic product example (from this directory):

```sh
python3 -B - <<'PYTHON'
import sys
sys.path.insert(0, 'tests')
import synthetic
synthetic.photometry_zip('/tmp/ctlux-package.zip')
PYTHON
python3 -B -m core products /tmp/ctlux-package.zip /tmp/ctlux-products
```

Synthetic glare example: a 4 x 5 x 3 m room with one ceiling light, viewed from
the side. The light is found at about 21.8 degrees and named `ceiling light`.

```sh
python3 -B - <<'PYTHON'
import sys
sys.path.insert(0, 'tests')
import synthetic
synthetic.glare_room('/tmp/ctlux-room.evo')
PYTHON
python3 -B -m core /tmp/ctlux-room.evo /tmp/ctlux-room
python3 -B -m core glare /tmp/ctlux-room /tmp/ctlux-glare --vp 0.8 2.5 1.2 --vd 1 0 0.6 --quality draft
```

Run tests with `python3 -B -m unittest discover -s tests -v`. There are 22 test
modules. Optional dependency skips are explicit. The glare command tests are
skipped when Radiance or evalglare is missing. `test_core` checks
prohibited imports throughout every core subdirectory and runs conversion and
`products` with write-audit checks. `test_glare` does the same for `glare`.

Method notes:

- “LS10 project file grammar: method notes”:
  https://github.com/JamesAugustus/ctlux-ls10
- “EVO lighting layout recovery for Radiance: method notes”:
  https://github.com/JamesAugustus/ctlux-evo

Licence: choose either the MIT License or the Apache License, Version 2.0
(`MIT OR Apache-2.0`). See [LICENSE](LICENSE), [LICENSE-MIT](LICENSE-MIT)
and [LICENSE-APACHE](LICENSE-APACHE). Both choices permit commercial use,
modification and distribution in products whose source remains private.
Neither choice requires publication of your source code.

Under MIT, include the copyright and permission notice in all copies or
substantial portions of the software. Under Apache 2.0, redistribution requires
a copy of the licence, prominent notices in changed files, retention of relevant
copyright, patent, trademark and attribution notices in distributed source,
and the relevant [NOTICE](NOTICE) attribution in one of the forms allowed by
section 4. The Apache NOTICE requirement applies when choosing Apache 2.0.
Choosing MIT does not impose Apache conditions.

Neither choice requires a credit in the user interface or an academic citation.
Citation using [CITATION.cff](CITATION.cff) is appreciated and voluntary.
These licences cover the original CTLux code and documentation. External tools
and user supplied project or photometry data retain their own terms.

Development note: the design, the mathematics and the analysis of the file formats are the author's. The author usually works in C. AI tools helped write and review the Python code and keep the documents organised.
