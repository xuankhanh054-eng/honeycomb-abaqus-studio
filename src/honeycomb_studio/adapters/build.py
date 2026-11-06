# -*- coding: utf-8 -*-
"""Abaqus 2017 / Python 2.7. Run: abaqus cae noGUI=build_honeycomb_7J.py
Reconstruction of Foo, Seah & Chai, Composite Structures 85 (2008) 20-28.
Units: mm, tonne, second, N, MPa. 1 J = 1000 N.mm.
All numerical choices absent from the article are reported in model_manifest.json.
"""
from abaqus import *
from abaqusConstants import *
import part, material, section, assembly, step, interaction, load, mesh, job
import regionToolset, visualization
import displayGroupMdbToolset as dgm
import math, json, os, time, sys

def byteify(value):
    if isinstance(value, unicode): return value.encode('utf-8')
    if isinstance(value, dict): return dict((byteify(k),byteify(v)) for k,v in value.items())
    if isinstance(value, list): return [byteify(v) for v in value]
    return value
CONFIG = os.path.abspath(sys.argv[-1])
P = byteify(json.load(open(CONFIG)))
OUT = os.path.abspath(P['output_directory'])
if not os.path.isdir(OUT): os.makedirs(OUT)
os.chdir(OUT)
with open('build_progress.txt','w') as f: f.write('Starting reconstruction\n')
MODEL = 'Honeycomb-Study'
JOB = P['job_name']
IS_REENTRANT=P.get('core_topology','hexagonal')=='reentrant_0'
IS_CONE=P.get('impactor_shape','sphere')=='blunt_cone'
HAS_DAMAGE=bool(P['core_material'].get('johnson_cook') or P['face_material'].get('johnson_cook'))
FACE_MATERIAL='AA6061-T6-Facesheets' if P['face_material'].get('johnson_cook') else 'Al1100-H14-Facesheets'
CORE_MATERIAL='AA6061-T6-Core' if P['core_material'].get('johnson_cook') else 'Al3003-H19-Core'
if not IS_REENTRANT and P.get('hex_geometry','regular')=='regular':
    P['side']=P['cell_size']/math.sqrt(3.0)
    P['periodic_density_kg_m3']=8.0*P['foil']*P['core_material']['density']/(3.0*P['cell_size'])

def log(s):
    with open('build_progress.txt','a') as f: f.write(s+'\n')
    print(s)

def clipped(a,b):
    # Liang-Barsky: the cut core follows the 100 x 100 mm facesheet perimeter.
    dx=b[0]-a[0]; dy=b[1]-a[1]; lo=0.; hi=1.; h=P['panel']/2.
    for p,q in [(-dx,a[0]+h),(dx,h-a[0]),(-dy,a[1]+h),(dy,h-a[1])]:
        if abs(p)<1.e-12:
            if q<0: return None
        else:
            r=q/p
            if p<0: lo=max(lo,r)
            else: hi=min(hi,r)
    if hi-lo<1.e-8: return None
    u=tuple(round(a[k]+lo*(b[k]-a[k]),7) for k in (0,1))
    v=tuple(round(a[k]+hi*(b[k]-a[k]),7) for k in (0,1))
    if math.hypot(u[0]-v[0],u[1]-v[1])<P.get('min_segment_length',.05): return None
    return tuple(sorted((u,v)))

Mdb()
m=mdb.Model(name=MODEL,description=('%s; %%g J; mm-tonne-s-N; see manifest.'%('Assumed reentrant image-1 core; baseline external setup' if IS_REENTRANT else 'Foo2008 reconstruction'))%P['energy_J'])
if 'Model-1' in mdb.models.keys(): del mdb.models['Model-1']
for name,props,table in [(FACE_MATERIAL,P['face_material'],P['face_plastic_table']),(CORE_MATERIAL,P['core_material'],P['core_plastic_table'])]:
    mat=m.Material(name=name)
    mat.Density(table=((props['density']*1.e-12,),))
    mat.Elastic(table=((props['young'],props['poisson']),))
    mat.Plastic(table=tuple(tuple(v) for v in table))
steel=m.Material(name='Steel-Rigid-EffectiveDensity')
steel_density=2.25e-6*(P['impact_mass']/.00265)*(13.1/P['ball_diameter'])**3
steel.Density(table=((steel_density,),))
steel.Elastic(table=((P.get('impactor_young',200000.),P.get('impactor_poisson',.3)),))
m.HomogeneousSolidSection(name='SteelSolid',material=steel.name)
for name,mat,t in [('Top-Facesheet-Section',FACE_MATERIAL,P['skin']),
                   ('Single-Foil-Section',CORE_MATERIAL,P['foil']),
                   ('Double-Foil-Section',CORE_MATERIAL,2.*P['foil'])]:
    m.HomogeneousShellSection(name=name,material=mat,thickness=t,
                             integrationRule=SIMPSON,numIntPts=P['shell_points'])
m.HomogeneousShellSection(name='Bottom-Facesheet-Section',material=FACE_MATERIAL,thickness=P['bottom_skin'],integrationRule=SIMPSON,numIntPts=P['shell_points'])

# Vertical opposite sides have double foil thickness, consistent with Fig. 7
# and Eq. 8. Unique shared walls, rather than overlapping duplicated shells.
angle=math.radians(P['core_angle']); co=math.cos(angle); si=math.sin(angle)
if IS_REENTRANT:
    sys.path.insert(0,P['adapter_directory'])
    from reentrant import network, bond_faces
    walls=network(P['panel'],P['core_height'],P['cell_size'],P['reentrant_cell_height'],
                  P['reentrant_indent'],P['phase_x'],P['phase_z'],P['min_segment_length'])
elif P.get('hex_geometry','regular')=='parametric':
    sys.path.insert(0,P['adapter_directory'])
    from hexagonal import network
    walls=network(P['panel'],P['hex_b'],P['hex_l'],P['hex_theta_deg'],
                  P['core_angle'],P['phase_x'],P['phase_y'],clipped)
else:
    walls=set(); b=P['side']; D=P['cell_size']
    extent=int(math.ceil(P['panel']*1.5/min(D,1.5*b)))+3
    for row in range(-extent,extent+1):
        for col in range(-extent,extent+1):
            cx=D*(col+0.5*(row%2)); cy=1.5*b*row
            pts=[(cx+b*math.cos(math.pi/6.+k*math.pi/3.),
                  cy+b*math.sin(math.pi/6.+k*math.pi/3.)) for k in range(6)]
            pts=[(x*co-y*si+P['phase_x'],x*si+y*co+P['phase_y']) for x,y in pts]
            for k in range(6):
                seg=clipped(pts[k],pts[(k+1)%6])
                if seg: walls.add(seg)
    walls=sorted(walls)
s=m.ConstrainedSketch(name='ReentrantXZNetwork' if IS_REENTRANT else 'HexagonalNetwork',sheetSize=max(250.,P['panel']*2.5))
for wall in walls: s.Line(point1=wall[0],point2=wall[1])
core=m.Part(name='Honeycomb-Core',dimensionality=THREE_D,type=DEFORMABLE_BODY)
core.BaseShellExtrude(sketch=s,depth=P['panel'] if IS_REENTRANT else P['core_height'])
single=[]; double=[]
for f in core.faces:
    n=f.getNormal()
    isdouble=abs(n[1])>0.99999 if IS_REENTRANT else abs(n[0]*co+n[1]*si)>0.99999
    (double if isdouble else single).append(f.index)
# Geometry arrays are selected by index; sequenceFromLabels is a mesh API.
singlefaces=core.faces[0:0]; doublefaces=core.faces[0:0]
for i in single: singlefaces+=core.faces[i:i+1]
for i in double: doublefaces+=core.faces[i:i+1]
core.Set(name='SINGLE_FOIL',faces=singlefaces)
core.Set(name='DOUBLE_FOIL',faces=doublefaces)
core.SectionAssignment(region=core.sets['SINGLE_FOIL'],sectionName='Single-Foil-Section')
core.SectionAssignment(region=core.sets['DOUBLE_FOIL'],sectionName='Double-Foil-Section')
if IS_REENTRANT:
    h=P['core_height']/2.
    bond_top_y=h; bond_bottom_y=-h
    topedges=core.edges.getByBoundingBox(yMin=h-.00001,yMax=h+.00001)
    botedges=core.edges.getByBoundingBox(yMin=-h-.00001,yMax=-h+.00001)
else:
    topedges=core.edges.getByBoundingBox(zMin=P['core_height']-.00001,zMax=P['core_height']+.00001)
    botedges=core.edges.getByBoundingBox(zMin=-.00001,zMax=.00001)
P['core_end_bond_selection']={}
for end,edges,target in [('TOP',topedges,P['core_height']/2.),('BOTTOM',botedges,-P['core_height']/2.)]:
    if len(edges)>0:
        core.Surface(name=end+'_BOND',side1Edges=edges)
        P['core_end_bond_selection'][end]={'kind':'nominal_edges','gap_mm':0.}
    elif IS_REENTRANT:
        points=[v.pointOn[0] for v in core.vertices]
        vertices=[f.getVertices() for f in core.faces]
        coordinate,indices=bond_faces(points,vertices,target,P['tie_tolerance'])
        endfaces=core.faces[0:0]
        for i in indices: endfaces+=core.faces[i:i+1]
        core.Surface(name=end+'_BOND',side12Faces=endfaces)
        if end=='TOP': bond_top_y=coordinate
        else: bond_bottom_y=coordinate
        P['core_end_bond_selection'][end]={'kind':'end_cap_faces','coordinate_mm':coordinate,
            'gap_mm':abs(target-coordinate),'tie_tolerance_mm':P['tie_tolerance'],'face_count':len(indices)}
        log('CORE_BOND %s: %d complete end faces; gap %.9g mm within Tie %.9g mm'%
            (end,len(indices),abs(target-coordinate),P['tie_tolerance']))
    else:
        raise ValueError('Missing nominal core bond edges')
core.seedPart(size=P['core_outer_seed'],deviationFactor=0.1,minSizeFactor=0.1)
for e in core.edges:
    v=e.getVertices()
    if len(v)!=2: continue
    a=core.vertices[v[0]].pointOn[0]; c=core.vertices[v[1]].pointOn[0]
    extrusion=P['panel'] if IS_REENTRANT else P['core_height']
    if abs(c[2]-a[2])>extrusion*.99:
        divisions=max(1,int(math.ceil(P['panel']/P['reentrant_depth_seed']))) if IS_REENTRANT else P['core_z_divisions']
        core.seedEdgeByNumber(edges=(e,),number=divisions,constraint=FIXED)
    else:
        rad=abs((a[0]+c[0])*0.5-P['impact_x']) if IS_REENTRANT else math.hypot((a[0]+c[0])*0.5-P['impact_x'],(a[1]+c[1])*0.5-P['impact_y'])
        size=P['core_centre_seed'] if rad<P['fine_radius'] else P['core_outer_seed']
        number=max(1,int(math.ceil(e.getSize(printResults=False)/size)))
        core.seedEdgeByNumber(edges=(e,),number=number,constraint=FIXED)
core.setMeshControls(regions=core.faces,elemShape=QUAD,technique=STRUCTURED)
shelltypes=(mesh.ElemType(elemCode=S4R,elemLibrary=EXPLICIT),
            mesh.ElemType(elemCode=S3R,elemLibrary=EXPLICIT))
core.setElementType(regions=(core.faces,),elemTypes=shelltypes)
core.generateMesh()
if IS_REENTRANT:
    node_y=[node.coordinates[1] for node in core.nodes]
    ends={'TOP':[],'BOTTOM':[]}; h=P['core_height']/2.
    for elem in core.elements:
        ys=[node_y[i] for i in elem.connectivity]
        if max(ys)>bond_top_y-1.e-5: ends['TOP'].append(elem.label)
        if min(ys)<bond_bottom_y+1.e-5: ends['BOTTOM'].append(elem.label)
    for end in ('TOP','BOTTOM'):
        core.Surface(name=end+'_BOND_CONTACT_EXCLUSION',side12Elements=core.elements.sequenceFromLabels(ends[end]))
    P['bond_contact_treatment']='Exclude core end-row contact with the tied facesheet only; core self-contact remains active'
log('CORE_MESH %d elements %d nodes'%(len(core.elements),len(core.nodes)))

s=m.ConstrainedSketch(name='SquareFacesheet',sheetSize=250.)
s.rectangle(point1=(-P['panel']/2.,-P['panel']/2.),point2=(P['panel']/2.,P['panel']/2.))
skin=m.Part(name='Facesheet',dimensionality=THREE_D,type=DEFORMABLE_BODY)
skin.BaseShell(sketch=s)
CLAMP_MODE=P.get('clamp_mode','circular')
CLAMP_SET='PERIMETER_CLAMP' if CLAMP_MODE=='perimeter' else 'CIRCULAR_CLAMP'
sk=m.ConstrainedSketch(name='FacesheetPartition',sheetSize=250.)
if CLAMP_MODE=='circular':
    sk.CircleByCenterPerimeter(center=(0.,0.),point1=(P['clamp_diameter']/2.,0.))
q=P['skin_patch_half']; ix=P['impact_x']; iy=P['impact_y']
sk.rectangle(point1=(ix-q,iy-q),point2=(ix+q,iy+q))
skin.PartitionFaceBySketch(faces=skin.faces,sketch=sk)
skin.Set(name='ALL',faces=skin.faces)
skin.SectionAssignment(region=skin.sets['ALL'],sectionName='Top-Facesheet-Section',offsetType=MIDDLE_SURFACE)
skin.Surface(name='POSITIVE_Z',side1Faces=skin.faces)
skin.Surface(name='NEGATIVE_Z',side2Faces=skin.faces)
skin.Surface(name='BOTH_SIDES',side12Faces=skin.faces)
skin.seedPart(size=P['skin_outer_seed'],deviationFactor=.05,minSizeFactor=.2)
for e in skin.edges:
    x,y,z=e.pointOn[0]
    if max(abs(x-ix),abs(y-iy))<=q+.01:
        skin.seedEdgeBySize(edges=(e,),size=P['skin_centre_seed'],constraint=FINER)
skin.setMeshControls(regions=skin.faces,elemShape=QUAD_DOMINATED,technique=FREE,algorithm=ADVANCING_FRONT)
skin.setElementType(regions=(skin.faces,),elemTypes=shelltypes)
skin.generateMesh()
if CLAMP_MODE=='perimeter':
    labels=[n.label for n in skin.nodes if abs(max(abs(n.coordinates[0]),abs(n.coordinates[1]))-P['panel']/2.)<=1.e-5]
else:
    labels=[n.label for n in skin.nodes if math.hypot(n.coordinates[0],n.coordinates[1])>=P['clamp_diameter']/2.-1.e-5]
skin.Set(name=CLAMP_SET,nodes=skin.nodes.sequenceFromLabels(labels))
log('SKIN_MESH %d elements %d nodes'%(len(skin.elements),len(skin.nodes)))

R=P['ball_diameter']/2.
if IS_CONE:
    s=m.ConstrainedSketch(name='BluntConeProfile',sheetSize=160.)
    s.ConstructionLine(point1=(0.,-10.),point2=(0.,80.))
    points=[(0.,0.),(P['nose_diameter_mm']/2.,0.),
            (P['shank_diameter_mm']/2.,P['cone_height_mm']),
            (P['shank_diameter_mm']/2.,P['cone_height_mm']+P['shank_height_mm'])]
    for left,right in zip(points[:-1],points[1:]): s.Line(point1=left,point2=right)
    ball=m.Part(name='Rigid-Impactor',dimensionality=THREE_D,type=ANALYTIC_RIGID_SURFACE)
    ball.AnalyticRigidSurfRevolve(sketch=s)
    ball.Surface(name='CONTACT',side1Faces=ball.faces)
else:
    s=m.ConstrainedSketch(name='SphereProfile',sheetSize=40.)
    s.ConstructionLine(point1=(0.,-2.*R),point2=(0.,2.*R))
    s.ArcByCenterEnds(center=(0.,0.),point1=(0.,R),point2=(0.,-R),direction=CLOCKWISE)
    s.Line(point1=(0.,-R),point2=(0.,R))
    ball=m.Part(name='Rigid-Impactor',dimensionality=THREE_D,type=DEFORMABLE_BODY)
    ball.BaseSolidRevolve(sketch=s,angle=360.)
    ball.Set(name='ALL_CELLS',cells=ball.cells)
    ball.SectionAssignment(region=ball.sets['ALL_CELLS'],sectionName='SteelSolid')
    ball.seedPart(size=P['ball_seed'],deviationFactor=.05,minSizeFactor=.1)
    ball.setMeshControls(regions=ball.cells,elemShape=TET,technique=FREE)
    ball.setElementType(regions=(ball.cells,),elemTypes=(mesh.ElemType(elemCode=C3D4,elemLibrary=EXPLICIT),))
    ball.generateMesh()
    ball.Set(name='RIGID_ELEMENTS',elements=ball.elements)
rp=ball.ReferencePoint(point=(0.,0.,0.))
ball.Set(name='RP',referencePoints=(ball.referencePoints[rp.id],))
# Correct the faceted tetrahedral sphere mass without changing the published
# effective density. The additional point mass is explicitly documented.
volume=0.
for e in ([] if IS_CONE else ball.elements):
    ns=[ball.nodes[i].coordinates for i in e.connectivity]
    a=[ns[1][i]-ns[0][i] for i in range(3)]
    c=[ns[2][i]-ns[0][i] for i in range(3)]
    d=[ns[3][i]-ns[0][i] for i in range(3)]
    volume+=abs(a[0]*(c[1]*d[2]-c[2]*d[1])-a[1]*(c[0]*d[2]-c[2]*d[0])+a[2]*(c[0]*d[1]-c[1]*d[0]))/6.
P['impactor_mesh_volume_mm3']=volume
P['impactor_mesh_mass_tonne']=volume*steel_density
P['impactor_correction_mass_tonne']=P['impact_mass']-P['impactor_mesh_mass_tonne']
assert P['impactor_correction_mass_tonne']>0
ball.engineeringFeatures.PointMassInertia(name='Impactor-MassCorrection',region=ball.sets['RP'],mass=P['impactor_correction_mass_tonne'])

a=m.rootAssembly; a.DatumCsysByDefault(CARTESIAN)
a.Instance(name='Core-1',part=core,dependent=ON)
if IS_REENTRANT:
    a.rotate(instanceList=('Core-1',),axisPoint=(0.,0.,0.),axisDirection=(1.,0.,0.),angle=90.)
    a.translate(instanceList=('Core-1',),vector=(0.,P['panel']/2.,P['core_height']/2.))
bottom=m.Part(name='Bottom-Facesheet',objectToCopy=skin)
bottom.SectionAssignment(region=bottom.sets['ALL'],sectionName='Bottom-Facesheet-Section',offsetType=MIDDLE_SURFACE)
a.Instance(name='Bottom-1',part=bottom,dependent=ON)
a.Instance(name='Top-1',part=skin,dependent=ON)
a.Instance(name='Impactor-1',part=ball,dependent=ON)
if IS_CONE:
    a.rotate(instanceList=('Impactor-1',),axisPoint=(0.,0.,0.),axisDirection=(1.,0.,0.),angle=90.)
a.translate(instanceList=('Bottom-1',),vector=(0.,0.,-P['bottom_skin']/2.))
a.translate(instanceList=('Top-1',),vector=(0.,0.,P['core_height']+P['skin']/2.))
a.translate(instanceList=('Impactor-1',),vector=(P['impact_x'],P['impact_y'],P['core_height']+P['skin']+P['gap']+(0. if IS_CONE else R)))
for face,edge,instance,masterface in [('Top','TOP_BOND','Top-1','NEGATIVE_Z'),('Bottom','BOTTOM_BOND','Bottom-1','POSITIVE_Z')]:
    m.Tie(name=face+'-PerfectBond',master=a.instances[instance].surfaces[masterface],
          slave=a.instances['Core-1'].surfaces[edge],positionToleranceMethod=SPECIFIED,
          positionTolerance=P['tie_tolerance'],adjust=ON,tieRotations=ON,thickness=ON)
if IS_CONE:
    m.RigidBody(name='Impactor-RigidBody',refPointRegion=a.instances['Impactor-1'].sets['RP'],surfaceRegion=a.instances['Impactor-1'].surfaces['CONTACT'])
else:
    m.RigidBody(name='Impactor-RigidBody',refPointRegion=a.instances['Impactor-1'].sets['RP'],bodyRegion=a.instances['Impactor-1'].sets['RIGID_ELEMENTS'])
for name in ['Bottom-1','Top-1']:
    m.EncastreBC(name='Clamp-'+name,createStepName='Initial',region=a.instances[name].sets[CLAMP_SET])
m.DisplacementBC(name='Guided-Z-Impactor',createStepName='Initial',region=a.instances['Impactor-1'].sets['RP'],u1=0.,u2=0.,u3=UNSET,ur1=0.,ur2=0.,ur3=0.)
m.ExplicitDynamicsStep(name='Impact',previous='Initial',timePeriod=P['duration'],nlgeom=ON,linearBulkViscosity=P['bulk_linear'],quadBulkViscosity=P['bulk_quadratic'])
m.Velocity(name='Initial-Impact-Velocity',region=a.instances['Impactor-1'].sets['RP'],velocity3=-P['velocity'])
if P['gravity'] and not IS_CONE:
    m.Gravity(name='Gravity-Impactor',createStepName='Impact',comp3=-P['gravity']*1000.,region=a.instances['Impactor-1'].sets['RIGID_ELEMENTS'])
# The point mass correction receives the same gravitational acceleration.
if P['gravity']:
    m.ConcentratedForce(name='Gravity-MassCorrection',createStepName='Impact',region=a.instances['Impactor-1'].sets['RP'],cf3=-P['gravity']*1000.*P['impactor_correction_mass_tonne'])
cp=m.ContactProperty('Frictionless-HardContact')
cp.NormalBehavior(pressureOverclosure=HARD,allowSeparation=ON,constraintEnforcementMethod=DEFAULT)
if P['friction']:
    # CAE 2017's API defaults fraction to zero, which PENALTY rejects even
    # for Explicit. Set the documented contact slip tolerance explicitly.
    cp.TangentialBehavior(formulation=PENALTY,table=((P['friction'],),),
                          maximumElasticSlip=FRACTION,fraction=.005)
else:
    cp.TangentialBehavior(formulation=FRICTIONLESS)
gc=m.ContactExp(name='GeneralContact-SelfContact',createStepName='Initial')
gc.includedPairs.setValuesInStep(stepName='Initial',useAllstar=ON)
gc.contactPropertyAssignments.appendInStep(stepName='Initial',assignments=((GLOBAL,SELF,cp.name),))
exclusions=[]
if not P['skin_self_contact']:
    exclusions.extend(((a.instances['Top-1'].surfaces['BOTH_SIDES'],SELF),(a.instances['Bottom-1'].surfaces['BOTH_SIDES'],SELF)))
if IS_REENTRANT:
    exclusions.extend(((a.instances['Core-1'].surfaces['TOP_BOND_CONTACT_EXCLUSION'],a.instances['Top-1'].surfaces['BOTH_SIDES']),
                       (a.instances['Core-1'].surfaces['BOTTOM_BOND_CONTACT_EXCLUSION'],a.instances['Bottom-1'].surfaces['BOTH_SIDES'])))
if exclusions: gc.excludedPairs.setValuesInStep(stepName='Initial',addPairs=tuple(exclusions))
ELEMENT_FIELDS=('S','PE','PEEQ','LE','STATUS','SDEG','DMICRT','JCCRT','TEMP')
nodal=[v for v in P['field_variables'] if v not in ELEMENT_FIELDS]
m.fieldOutputRequests['F-Output-1'].setValues(variables=tuple(nodal),numIntervals=P['field_intervals'])
a.Set(name='SANDWICH_ELEMENTS',elements=(a.instances['Core-1'].elements,a.instances['Top-1'].elements,a.instances['Bottom-1'].elements))
a.Set(name='SANDWICH_NODES',nodes=(a.instances['Core-1'].nodes,a.instances['Top-1'].nodes,a.instances['Bottom-1'].nodes))
stress=[v for v in P['field_variables'] if v in ELEMENT_FIELDS]
if stress:
    m.FieldOutputRequest(name='Sandwich-Stress-PlasticStrain',createStepName='Impact',variables=tuple(stress),region=a.sets['SANDWICH_ELEMENTS'],numIntervals=P['field_intervals'])
# The 2017 CAE API rejects ALLIHE for a mechanical Explicit step; the
# adiabatic keyword and complete solver energy request are injected together.
cae_history=[v for v in P['history_variables'] if not (HAS_DAMAGE and v=='ALLIHE')]
m.historyOutputRequests['H-Output-1'].setValues(variables=tuple(cae_history),numIntervals=P['history_intervals'])
if HAS_DAMAGE:
    for instance,group in [('Core-1','CORE'),('Top-1','FRONT'),('Bottom-1','REAR')]:
        a.Set(name=group+'_ENERGY',elements=a.instances[instance].elements)
        m.HistoryOutputRequest(name=group+'-Energy',createStepName='Impact',variables=('ALLPD','ALLDMD','ALLSE','ALLAE','ALLIE'),region=a.sets[group+'_ENERGY'],numIntervals=P['history_intervals'])
m.HistoryOutputRequest(name='Impactor-History',createStepName='Impact',variables=('U3','V3','A3','RF3'),region=a.instances['Impactor-1'].sets['RP'],numIntervals=P['history_intervals'])
P['measurement_nodes']={}
for face in ['Top-1','Bottom-1']:
    nodes=a.instances[face].nodes
    node=min(nodes,key=lambda n: (n.coordinates[0]-P['impact_x'])**2+(n.coordinates[1]-P['impact_y'])**2)
    P['measurement_nodes'][face]={'label':node.label,'coordinates_mm':list(node.coordinates),'distance_from_impact_xy_mm':math.hypot(node.coordinates[0]-P['impact_x'],node.coordinates[1]-P['impact_y'])}
    a.Set(name=face+'-CentreNode',nodes=nodes.sequenceFromLabels((node.label,)))
    m.HistoryOutputRequest(name=face+'-Centre',createStepName='Impact',variables=('U3',),region=a.sets[face+'-CentreNode'],numIntervals=P['history_intervals'])
mdb.Job(name=JOB,model=MODEL,type=ANALYSIS,numCpus=P['cpus'],numDomains=P['cpus'],
        explicitPrecision=DOUBLE,nodalOutputPrecision=FULL,memory=70,memoryUnits=PERCENTAGE,
        description='Reconstructed Foo2008; %g J; see model_manifest.json'%P['energy_J'])
# Preserve full physical skin contact thickness under mesh refinement. Skin
# self-contact is unnecessary for this low-energy intact-face impact. Core
# self-contact remains active. Abaqus 2017 exposes this option via keyword editor.
m.keywordBlock.synchVersions(storeNodesAndElements=False)
if P.get('explicit_continuation',False):
    # Output control only: never force smaller increments at checkpoint times.
    for i,block in enumerate(m.keywordBlock.sieBlocks):
        if block.lower().startswith('*restart'):
            m.keywordBlock.replace(i,'*Restart, write, number interval=48, time marks=NO, overlay')
            break
    else:
        raise RuntimeError('Continuation requires an Explicit restart output request')
    P['continuation_restart_intervals']=48
for i,block in enumerate(m.keywordBlock.sieBlocks):
    if block.lower().startswith('*contact property assignment'):
        m.keywordBlock.insert(i,'*Contact Controls Assignment, CONTACT THICKNESS REDUCTION=NOPERIMSELF')
        break
P['contact_thickness_control']='NOPERIMSELF; skin self-contact '+('enabled' if P['skin_self_contact'] else 'excluded')
P['impactor_effective_density_tonne_mm3']=steel_density
P['actual_mesh_gap_mm']=P['gap'] if IS_CONE else min(n.coordinates[2] for n in a.instances['Impactor-1'].nodes)-(P['core_height']+P['skin'])
mdb.saveAs(pathName=os.path.join(OUT,JOB+'.cae'))
mdb.jobs[JOB].writeInput(consistencyChecking=ON)
if HAS_DAMAGE:
    sys.path.insert(0,P['adapter_directory'])
    from damage_keywords import rewrite_input
    rewrite_input(JOB+'.inp',P,{FACE_MATERIAL:P['face_material'],CORE_MATERIAL:P['core_material']})
P['elements']={'core':len(core.elements),'facesheet_each':len(skin.elements),'impactor':len(ball.elements)}
P['walls']={'single':len(single),'double':len(double),'total':len(walls)}
P['core_cut_density_kg_m3']=sum([f.getSize(printResults=False)*(2.*P['foil'] if f.index in double else P['foil']) for f in core.faces])*P['core_material']['density']/(P['panel']**2*P['core_height'])
P['core_mass_kg']=P['core_cut_density_kg_m3']*P['panel']**2*P['core_height']*1.e-9
P['panel_mass_kg']=P['panel']**2*((P['skin']+P['bottom_skin'])*P['face_material']['density']+P['core_height']*P['core_cut_density_kg_m3'])*1.e-9
with open('model_manifest.json','w') as f: json.dump(P,f,indent=2,sort_keys=True)
with open('core_segments.json','w') as f: json.dump(walls,f)
# A preview generated by the actual CAE viewport (no solver deformation).
vp=session.viewports[session.currentViewportName]
vp.setValues(displayedObject=a)
vp.assemblyDisplay.setValues(mesh=OFF)
vp.view.setValues(cameraPosition=(170.,-200.,155.),cameraUpVector=(0.,0.,1.),cameraTarget=(0.,0.,10.))
vp.view.fitView()
session.printOptions.setValues(vpDecorations=OFF)
session.printToFile(fileName='model_isometric',format=PNG,canvasObjects=(vp,))
vp.assemblyDisplay.displayGroup.remove(leaf=dgm.LeafFromInstance(instances=(a.instances['Top-1'],)))
vp.view.fitView()
session.printToFile(fileName='core_visible',format=PNG,canvasObjects=(vp,))
vp.assemblyDisplay.displayGroup.add(leaf=dgm.LeafFromInstance(instances=(a.instances['Top-1'],)))
mdb.save()
log('BUILD_COMPLETE')
