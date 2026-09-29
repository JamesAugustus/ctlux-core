# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Entirely synthetic EVO records, with no user files or project dimensions."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
from core import engine as engine
from core import importer as importer
from core import format_detector as format_detector

# The storey is rotated and translated; its contour and luminaire use local coordinates.
STEP='''
#1=CoordSys3D((10,20,4),(0,1,0),(-1,0,0),(0,0,1));
#2=CoordSys3D((0,0,0),(1,0,0),(0,1,0),(0,0,1));
#3=CoordSys3D((1,0,0),(1,0,0),(0,1,0),(0,0,1));
#4=CoordSys3D((2,0,1),(1,0,0),(0,1,0),(0,0,1));
#5=CoordSys3D((1,0,0),(0,0,-1),(0,1,0),(1,0,0));
#10=Storey(10,'storey',(),#1,#11,$);
#11=InstanceGeometricRepresentation(#10,#12);
#12=StoreyRepresentationData(#10,0,(),(),False,(1,1,1),3);
#20=StoreyElement(20,'element',(),#2,$,$);
#30=StoreyContour(30,'contour',(),#3,#31,$);
#31=InstanceGeometricRepresentation(#30,#32);
#32=StoreyContourRepresentationData(#30,0,(),(),False,(1,1,1),((0,0),(4,0),(4,5),(0,5)),$,(),.Inner.,False,$,$);
#40=Space(40,'room',(),#2,#41,$);
#41=SpaceGeometricRepresentation(#40,#42);
#42=SpaceRepresentationData(#40,0,(#43),(),False,(1,1,1));
#43=StoreyContourBasedSpaceRepresentationDataPart(#42,1,#2,.Storey.,2.5);
#50=LuminaireArrangement(50,'array',(),#4,$,$);
#60=LuminaireElement(77,'Light α',(),#5,$,$);
#70=RelContainedInSpatialStructure(70,'',#10,(#20,#50,#60));
#71=RelAggregates(71,'',#20,(#30));
#72=RelAggregates(72,'',#10,(#40));
#73=RelAggregates(73,'',#50,(#60));
#74=RelAssociatesStoreyContourBasedSpace(74,'',#30,#40,True);
'''

POLYGON_STEP=STEP.replace('#43=StoreyContourBasedSpaceRepresentationDataPart(#42,1,#2,.Storey.,2.5);',
          '#43=PolygonBasedSpaceRepresentationDataPart(#42,3,(#81,#82,#83,#84));\n'
          '#81=PolyPoint2D($,(0,0));#82=PolyPoint2D($,(4,0));'
          '#83=PolyPoint2D($,(4,5));#84=PolyPoint2D($,(0,5));')

class EvoComponentTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ctlux-evo-components-')
        self.root=Path(self.tmp.name);self.src=self.root/'example.evo'
        self.pd=self.root/'project';(self.pd/'model').mkdir(parents=True)
        engine.save_project(str(self.pd),{'geometry':[],'luminaires':[]})
        self.write_zip()

    def tearDown(self): self.tmp.cleanup()

    def write_zip(self,step=STEP,extras=None):
        with zipfile.ZipFile(self.src,'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',step)
            for n,b in (extras or {}).items():z.writestr(n,b)

    def test_storey_array_element_composition_and_direction(self):
        a=engine.evo_luminaires(str(self.src))[0]
        self.assertEqual((a['x'],a['y'],a['z']),(10,23,5))
        self.assertEqual(a['direction'],[0,-1,0])

    def test_unverified_evo_color_is_neutral_and_other_light_data_unchanged(self):
        before = self.src.read_bytes()
        a = engine.evo_luminaires(str(self.src))[0]
        self.assertEqual(a['color'], [1, 1, 1])
        self.assertEqual(a['color_source'], 'default_neutral')
        self.assertIn('could not be verified', a['color_note'])
        self.assertEqual((a['name'], a['source']), ('luminaire_77', 'evo/STEP'))
        self.assertEqual((a['power'], a['radius'], a['type'], a['angle']), (25000, .18, 'spot', 90))
        self.assertEqual(a['direction'], [0, -1, 0])
        self.assertEqual(self.src.read_bytes(), before)

    def test_storey_contour_room_and_storey_height(self):
        target=self.pd/'model/room.rad';_,n=engine.evo_rooms(str(self.src),str(target))
        self.assertEqual(n,6)
        s=target.read_text();self.assertIn(' 10 21 4\n',s);self.assertIn(' 10 21 7\n',s)
        self.assertNotIn('6.5',s)  # Do not select the unused individual height of 2.5.

    def test_hierarchy_cycle_rejected(self):
        self.write_zip(STEP+'\n#80=RelAggregates(80,\'\',#60,(#10));')
        with self.assertRaisesRegex(ValueError,'Cycle'):engine.evo_luminaires(str(self.src))

    def test_invalid_contour_rejected(self):
        self.write_zip(STEP.replace('((0,0),(4,0),(4,5),(0,5))','((0,0),(4,0))'))
        with self.assertRaisesRegex(ValueError,'contour'):engine.evo_rooms(str(self.src),str(self.pd/'model/room.rad'))

    def test_fbx_error_does_not_block_step_data(self):
        self.write_zip(extras={'Mesh/toy.fbx':b'bad'})
        errors=[]
        with patch.object(engine,'convert',side_effect=RuntimeError('converter unavailable')),patch.object(engine,'evo_ies',return_value={}):
            gs,ls=format_detector._place_evo(str(self.src),str(self.pd),str(self.pd/'model/toy.obj'),[],errors)
        self.assertEqual(len(gs),1);self.assertEqual(len(ls),1)
        self.assertTrue(any('converter unavailable' in h for h in errors))

    def test_m3d_detail_reported_as_partial(self):
        self.write_zip(extras={'Mesh/toy.m3d':b'opaque','Mesh/toy.gdms':b'opaque'})
        errors=[]
        with patch.object(engine,'evo_ies',return_value={}):
            gs,ls=format_detector._place_evo(str(self.src),str(self.pd),str(self.pd/'model/toy.obj'),[],errors)
        self.assertEqual(len(gs),1);self.assertEqual(len(ls),1)
        self.assertTrue(any('not yet supported' in h for h in errors))

    def test_evo_step_does_not_require_assimp(self):
        with patch.object(importer.shutil,'which',return_value=None):self.assertTrue(engine.is_convertible('.evo'))

    def test_legacy_polygon_room_uses_storey_frame(self):
        step=POLYGON_STEP
        self.write_zip(step);target=self.pd/'model/room.rad'
        _,n=engine.evo_rooms(str(self.src),str(target));self.assertEqual(n,6)
        self.assertIn(' 10 20 4\n',target.read_text())

    def test_closed_polygon_has_six_nonzero_inward_faces(self):
        for refs in ('(#81,#82,#83,#84,#81)', '(#81,#84,#83,#82,#81)'):
            with self.subTest(refs=refs):
                step = POLYGON_STEP.replace('(#81,#82,#83,#84)', refs)
                self.assertEqual(self.room_normals(step), {'floor': 1, 'ceiling': 1, 'wall': 4})

    def test_polygon_with_fewer_than_three_valid_vertices_is_rejected_without_height_warning(self):
        for refs in ('(#81,#82)', '(#81,#82,#81)', '(#81,#82,#999)', '(#81,#82,#85)'):
            with self.subTest(refs=refs):
                step = POLYGON_STEP.replace('(#81,#82,#83,#84)', refs)
                self.write_zip(step + '\n#85=PolyPoint2D($,(1e309,5));')
                target = self.pd / 'model/room.rad'
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    with self.assertRaisesRegex(ValueError, 'polygon.*three valid vertices'):
                        engine.evo_rooms(str(self.src), str(target))
                self.assertEqual(caught, [])
                self.assertFalse(target.exists())

    def test_polygon_height_warns_that_first_eligible_scalar_is_unverified(self):
        self.write_zip(POLYGON_STEP.replace('#42,3,(', '#42,1.5,4.25,9,('))
        target = self.pd / 'model/room.rad'
        with self.assertWarnsRegex(RuntimeWarning,
                r'unverified: inferred 4\.25 m.*first scalar.*1\.5 < value < 30.*field has not been identified') as caught:
            _, count = engine.evo_rooms(str(self.src), str(target))
        self.assertEqual(len(caught.warnings), 1)
        self.assertEqual(count, 6)
        self.assertIn(' 10 20 8.25\n', target.read_text())
        self.assertNotIn(' 10 20 13\n', target.read_text())

    def test_polygon_height_warns_that_fallback_three_meters_is_unverified(self):
        self.write_zip(POLYGON_STEP.replace('#42,3,(', '#42,1.5,30,0,('))
        target = self.pd / 'model/room.rad'
        with self.assertWarnsRegex(RuntimeWarning,
                r'unverified: no scalar.*1\.5 < value < 30.*fallback height of 3 m') as caught:
            _, count = engine.evo_rooms(str(self.src), str(target))
        self.assertEqual(len(caught.warnings), 1)
        self.assertEqual(count, 6)
        self.assertIn(' 10 20 7\n', target.read_text())

    def room_normals(self,step):
        # Each polygon's Newell normal must point toward the room center: floor up, ceiling down.
        import re
        self.write_zip(step);target=self.pd/'model/room.rad'
        _, count = engine.evo_rooms(str(self.src),str(target))
        blocks=[(kind,[tuple(map(float,l.split())) for l in b.strip().split('\n')]) for kind,b in
                 re.findall(r'(\w+)_mat polygon \S+\n0\n0\n\d+\n((?: \S+ \S+ \S+\n)+)',target.read_text())]
        self.assertEqual(count, len(blocks))
        vertices=[v for _,p in blocks for v in p]
        center=[sum(v[i] for v in vertices)/len(vertices) for i in range(3)]
        result={}
        for kind,p in blocks:
            n=[0.,0.,0.]
            for k,(x,y,z) in enumerate(p):
                x2,y2,z2=p[(k+1)%len(p)]
                n[0]+=(y-y2)*(z+z2);n[1]+=(z-z2)*(x+x2);n[2]+=(x-x2)*(y+y2)
            self.assertGreater(sum(v * v for v in n), 0, (kind, p))
            midpoint=[sum(v[i] for v in p)/len(p) for i in range(3)]
            self.assertGreater(sum(n[i]*(center[i]-midpoint[i]) for i in range(3)),0,(kind,p))
            if kind=='floor':self.assertGreater(n[2],0)
            if kind=='ceiling':self.assertLess(n[2],0)
            if kind=='wall':self.assertAlmostEqual(n[2],0)
            result[kind]=result.get(kind,0)+1
        return result

    def test_room_faces_point_inward_for_both_windings_and_room_types(self):
        ccw,cw='((0,0),(4,0),(4,5),(0,5))','((0,0),(0,5),(4,5),(4,0))'
        contour={'counterclockwise':STEP,'clockwise':STEP.replace(ccw,cw)}
        polygon=POLYGON_STEP
        contour['polygon counterclockwise']=polygon
        contour['polygon clockwise']=polygon.replace('(#81,#82,#83,#84)','(#84,#83,#82,#81)')
        for name,step in contour.items():
            with self.subTest(name):
                self.assertEqual(self.room_normals(step),{'floor':1,'ceiling':1,'wall':4})

if __name__=='__main__':unittest.main()
