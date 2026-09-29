# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""STEP benzeri kayıtları sözcük düzeyinde ayırır, tırnak içindeki ayraçlar veridir."""
import warnings


def alanlar(body):
    out, pieces, start, depth, quoted, i = [], [], 0, 0, False, 0
    while i < len(body):
        c = body[i]
        if not quoted and c == "/" and body.startswith("*", i + 1):
            # Alanlar arasındaki yorum veri değildir, tek boşluk sayılır.
            close = body.find("*/", i + 2)
            if close < 0:
                raise ValueError("Kesilmiş STEP alanı")
            pieces += [body[start:i], " "]
            i = start = close + 2
            continue
        if c == "'":
            if quoted and i + 1 < len(body) and body[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError("STEP parantezi eşleşmiyor")
            elif c == "," and depth == 0:
                out.append("".join(pieces + [body[start:i]]).strip())
                pieces, start = [], i + 1
        i += 1
    if quoted or depth:
        raise ValueError("Kesilmiş STEP alanı")
    tail = "".join(pieces + [body[start:]]).strip()
    if tail:
        out.append(tail)
    return out


def kayitlar(text):
    """Tek yönde tara; kayıt adayı yalnız tırnak dışındaki noktalı virgülde biter."""
    out = {}
    i, quoted, pending, body_start = 0, False, None, None

    def atlanan(ident):
        warnings.warn("STEP kesik/bozuk kayıt atlandı: #" + str(ident),
                      RuntimeWarning, stacklevel=3)

    while i < len(text):
        c = text[i]
        if c == "'":
            if quoted and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if c == "/" and text.startswith("*", i + 1):
                # ISO 10303-21 yorumları tırnak dışındadır, içindeki apostrof metindir.
                close = text.find("*/", i + 2)
                i = len(text) if close < 0 else close + 2
                continue
            if body_start is not None and c == ";":
                # Dıştaki ')' kayda aittir, alan gövdesine dahil değildir.
                end = i
                while end > body_start and text[end - 1].isspace():
                    end -= 1
                if end > body_start and text[end - 1] == ")":
                    ident = int(pending)
                    if ident in out:
                        raise ValueError("Tekrarlanan STEP kayıt kimliği: %s" % ident)
                    body = text[body_start:end - 1]
                    try:
                        alanlar(body)
                    except ValueError:
                        atlanan(ident)
                    else:
                        out[ident] = (kind, body)
                else:
                    atlanan(pending)
                pending, body_start = None, None
            elif body_start is None and c == "#":
                # Başlık parçaları ayrıdır; bozuk girdide i geriye dönmez.
                start = i = i + 1
                while i < len(text) and text[i].isdecimal():
                    i += 1
                ident_end = i
                while i < len(text) and text[i].isspace():
                    i += 1
                if ident_end == start or i == len(text) or text[i] != "=":
                    continue
                if ident_end - start > 18:
                    # Daha uzun kimlik geçerli STEP kimliği değildir ve int() basamak sınırını aşar.
                    atlanan(text[start:start + 18] + "...")
                    continue
                pending = text[start:ident_end]
                i += 1
                while i < len(text) and text[i].isspace():
                    i += 1
                start = i
                while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                    i += 1
                kind_end = i
                while i < len(text) and text[i].isspace():
                    i += 1
                if kind_end == start or i == len(text) or text[i] != "(":
                    atlanan(pending)
                    pending = None
                    continue
                kind = text[start:kind_end]
                body_start = i + 1
        i += 1
    if pending is not None:
        atlanan(pending)
    return out
