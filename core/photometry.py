# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Find a luminaire's photometry file within the project boundary."""
import os
from core.file_safety import internal_path


def ies_source(project_dir, luminaire, library=None):
    """When library is supplied, also search <library>/luminaires. There is no default directory."""
    candidate = []
    if luminaire.get("ies"):
        name = luminaire["ies"]
        roots = [project_dir, os.path.join(project_dir, "light")]
        if library:
            roots.append(os.path.join(library, "luminaires"))
        for root in roots:
            try:
                candidate.append(internal_path(root, name))
            except ValueError:
                continue
    if luminaire.get("product_rad"):
        candidate.append(internal_path(project_dir, os.path.splitext(luminaire["product_rad"])[0] + ".ies"))
    return next((p for p in candidate if os.path.isfile(p)), None)


# Ownership notice added to every written IES file. LM-63-2002 keywords beginning with an underscore
# are user-defined. Use ASCII only because some IES readers fail on non-ASCII bytes. Line length <= 80.
NOTICE_PREFIX = "[_NOTICE] Photometric data belongs to its owner, normally the luminaire"
PROJECT_NOTICE = (NOTICE_PREFIX, "[MORE] manufacturer. Written from a lighting project file to rebuild",
             "[MORE] that project.")
FILE_NOTICE = (NOTICE_PREFIX, "[MORE] manufacturer. Copied from a file supplied by the user.")


def annotated_ies(data, lines=FILE_NOTICE):
    """Insert an ownership notice into IES bytes immediately before the TILT= line.

    Preserve source keywords and bytes and match its line endings.
    Return data unchanged if the header already contains the notice or has no TILT line.
    """
    text = data.decode("latin-1")
    chunk = text.splitlines(keepends=True)
    for i, line in enumerate(chunk):
        if line.strip().startswith(NOTICE_PREFIX):
            return data
        if line.strip().upper().startswith("TILT="):
            sample = chunk[i - 1] if i else line
            suffix = sample[len(sample.rstrip("\r\n")):] or "\n"
            inserted = "".join(s + suffix for s in lines)
            return "".join(chunk[:i] + [inserted] + chunk[i:]).encode("latin-1")
    return data
