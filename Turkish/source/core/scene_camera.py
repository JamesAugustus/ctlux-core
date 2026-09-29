# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""CTScene perspective kamera: metre, Z-up, dikey görüş açısı ve tam yönelim."""
import math
import struct


def _number(value):
    if type(value) not in (int, float) or not -1e30 < value < 1e30:
        raise ValueError('Kamera sayıları sonlu ve desteklenen aralıkta olmalı')
    return float(value)


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('Kamera vektörü üç sayı içermeli')
    return [_number(x) for x in value]


def _unit(v):
    size = math.hypot(*v)
    if size < 1e-12:
        raise ValueError('Kamera yönü sıfır veya yukarı vektörüne paralel olamaz')
    return [x / size for x in v]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def camera_normalize(camera):
    """Bağımsız kopya döner, up vektörünü bakış yönüne dik hale getirir, roll korunur."""
    if not isinstance(camera, dict) or camera.get('type') != 'perspective':
        raise ValueError('Kamera perspektif türünde olmalı')
    position = _vector(camera.get('position'))
    forward = _unit(_vector(camera.get('direction')))
    right = _unit(_cross(forward, _unit(_vector(camera.get('up')))))
    up = _cross(right, forward)
    yfov = _number(camera.get('yfov_degrees'))
    aspect = _number(camera.get('aspect_ratio'))
    near, far = _number(camera.get('znear')), _number(camera.get('zfar'))
    if not (0 < yfov < 179 and 0 < aspect <= 10000 and 0 < near < far):
        raise ValueError('Kamera görüş açısı, en/boy oranı veya kırpma aralığı geçersiz')
    near32, far32 = struct.unpack('ff', struct.pack('ff', near, far))
    aperture = math.tan(math.radians(yfov)/2)
    apertures = struct.unpack('ff', struct.pack('ff', aperture, aperture*aspect))
    if not (0 < near32 < far32) or not all(math.isfinite(x) and x > 0 for x in apertures):
        raise ValueError('Kamera lens/kırpma değerleri float32 aktarımında ayırt edilemiyor')
    return dict(type='perspective', position=position, direction=forward, up=up,
                yfov_degrees=yfov, aspect_ratio=aspect, znear=near, zfar=far)


def camera_basis(camera):
    c = camera_normalize(camera)
    forward, up = c['direction'], c['up']
    return _cross(forward, up), up, [-x for x in forward], c['position']


def radiance_view(camera, width, height, fisheye=False):
    """Dikey açı (yfov) aynen kullanılır, yatay açıyı çıktının en/boy oranı belirler.

    near/far analizde geometriyi kesmez, clipping sadece görsel çıktılar için
    geçerli. Kamera göndermeyen eski çağıranları çağıran taraf halleder.
    """
    c = camera_normalize(camera)
    if type(width) is not int or type(height) is not int or not (1 <= width <= 16384 and 1 <= height <= 16384):
        raise ValueError('Kamera çıktı boyutu 1..16384 olmalı')
    vv = c['yfov_degrees']
    vh = math.degrees(2*math.atan(math.tan(math.radians(vv)/2)*width/height))
    def vec(v):
        return ' '.join(format(x, '.12g') for x in v)
    if fisheye:
        vh = vv = 180
    return (('-vta' if fisheye else '-vtv') + ' -vp ' + vec(c['position']) + ' -vd ' + vec(c['direction']) +
            ' -vu ' + vec(c['up']) + ' -vh %.12g -vv %.12g' % (vh, vv))
