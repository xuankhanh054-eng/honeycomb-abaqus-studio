# -*- coding: utf-8 -*-
"""Image-1 core section, shared by Abaqus Python 2.7 and geometry verification."""
import math


def bond_faces(vertices, faces, target, tolerance):
    """Select existing end-cap faces within the prescribed perfect-bond tolerance.

    Clipping can remove very short inclined tails while retaining a horizontal
    end cap. Bind the complete cap using Tie adjust, without changing the wall
    network, shell mass, or the user's geometric and meshing inputs.
    """
    if not vertices or tolerance <= 0:
        raise ValueError('Missing core vertices or invalid bond tolerance')
    coordinate = max(p[1] for p in vertices) if target > 0 else min(p[1] for p in vertices)
    gap = abs(target - coordinate)
    if gap > tolerance + 1.e-7:
        raise ValueError('Core end lies outside the prescribed Tie tolerance')
    indices = [i for i, face in enumerate(faces)
               if face and all(abs(vertices[j][1] - coordinate) <= 1.e-5 for j in face)]
    if not indices:
        raise ValueError('No complete end-cap faces within the prescribed Tie tolerance')
    return coordinate, indices


def network(width, height, cell_width, cell_height, indent, phase_x=0., phase_z=0., min_length=.05):
    a, b = cell_width / 2., cell_height / 2.
    vertices = ((-a,-b),(a,-b),(a-indent,0.),(a,b),(-a,b),(-a+indent,0.))
    count = int(math.ceil((max(width,height)+abs(phase_x)+abs(phase_z))/min(cell_width-indent,cell_height)))+4
    walls = {}
    for n in range(-count,count+1):
        for row in range(-count,count+1):
            cx, cz = n*(cell_width-indent)+phase_x, n*b+cell_height*row+phase_z
            pts = [(x+cx,z+cz) for x,z in vertices]
            for k in range(6):
                u,v=pts[k],pts[(k+1)%6]
                dx,dz=v[0]-u[0],v[1]-u[1]
                lo,hi=0.,1.
                for p,q in ((-dx,u[0]+width/2.),(dx,width/2.-u[0]),(-dz,u[1]+height/2.),(dz,height/2.-u[1])):
                    if abs(p)<1.e-12:
                        if q < -1.e-10: hi=-1.; break
                    elif p<0.: lo=max(lo,q/p)
                    else: hi=min(hi,q/p)
                if hi-lo<1.e-9: continue
                p=tuple(round(u[i]+lo*(v[i]-u[i]),7) for i in (0,1))
                q=tuple(round(u[i]+hi*(v[i]-u[i]),7) for i in (0,1))
                if math.hypot(p[0]-q[0],p[1]-q[1])<min_length: continue
                if abs(p[1]-q[1])<1.e-7 and abs(abs(p[1])-height/2.)<1.e-7: continue
                segment=tuple(sorted((p,q)))
                double=k in (0,3)
                if segment in walls: assert walls[segment]==double
                walls[segment]=double
    return [(u,v,walls[(u,v)]) for u,v in sorted(walls)]
