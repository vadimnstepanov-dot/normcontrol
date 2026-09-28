"""Conservative raster proof for filled triangular heads on straight connectors.

No label semantics, reference answers or model confidence are used. Unsupported
open heads, curved/crossed connectors remain unresolved. Source images stay immutable.
"""
import math


def detect_heads(path):
    import cv2
    import numpy as np
    image=cv2.imread(str(path))
    if image is None:raise ValueError('Cannot read arrow source')
    h,w=image.shape[:2]
    if h*w>16_000_000:return []
    dark=(np.max(image,axis=2)<120).astype('uint8')*255
    thin=np.max(image,axis=2)<190
    contours,_=cv2.findContours(cv2.morphologyEx(dark,cv2.MORPH_OPEN,np.ones((2,2),np.uint8)),
                               cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    heads=[]
    for contour in contours:
        area=cv2.contourArea(contour);x,y,cw,ch=cv2.boundingRect(contour)
        if not 8<=area<=250 or min(cw,ch)<4 or max(cw,ch)>32:continue
        hull=cv2.convexHull(contour)
        triangle=cv2.approxPolyDP(hull,.10*cv2.arcLength(hull,True),True)
        if len(triangle)!=3:continue
        points=triangle[:,0].astype(float);center=points.mean(axis=0);directions=[]
        for angle in range(0,360,5):
            direction=np.array([math.cos(math.radians(angle)),math.sin(math.radians(angle))])
            projection=(points-center)@direction
            # A tip protrudes farther than the two-point base. This rejects a line arriving from the other side.
            if max(projection)<1.2*abs(min(projection)):continue
            votes=[]
            for distance in range(max(7,max(cw,ch)//2),max(7,max(cw,ch)//2)+26):
                xx,yy=np.rint(center-direction*distance).astype(int)
                votes.append(0<=xx<w and 0<=yy<h and bool(thin[max(0,yy-1):yy+2,max(0,xx-1):xx+2].any()))
            if sum(votes)/len(votes)>.90:directions.append(direction)
        if not directions:continue
        mean=np.mean(directions,axis=0)
        if np.linalg.norm(mean)<.95:continue # conflicting possible shafts
        mean/=np.linalg.norm(mean)
        tip=points[int(np.argmax((points-center)@mean))]
        heads.append(dict(bbox=[x/w,y/h,(x+cw)/w,(y+ch)/h],tip=[float(tip[0]/w),float(tip[1]/h)],
                          direction=mean.tolist(),center=(center/[w,h]).tolist(),method='triangle_and_shaft/1'))
    return heads


def verify_relations(page, image):
    """Attach separate pixel evidence; never mutate raw model observations."""
    try:heads=detect_heads(image)
    except ImportError:return []
    value=page.get('interpretation',{}).get('value',{})
    if value.get('kind')!='diagram':return []
    nodes={n['id']:n for n in value.get('nodes',[])};verified=[];used=set()
    for index,edge in enumerate(value.get('relations',[])):
        if edge['source'] not in nodes or edge['target'] not in nodes:continue
        box=edge['bbox'];matches=[]
        for i,head in enumerate(heads):
            x,y=head['center'];dx=max(box[0]-x,0,x-box[2]);dy=max(box[1]-y,0,y-box[3]);distance=math.hypot(dx,dy)
            if distance<.08:matches.append((distance,i,head))
        matches.sort(key=lambda t:t[0])
        if not matches or len(matches)>1 and matches[1][0]-matches[0][0]<.012:continue
        _,i,head=matches[0]
        if i in used:continue
        a=nodes[edge['source']]['bbox'];b=nodes[edge['target']]['bbox']
        delta=[(b[0]+b[2]-a[0]-a[2])/2,(b[1]+b[3]-a[1]-a[3])/2]
        norm=math.hypot(*delta)
        alignment=sum(x*y for x,y in zip(delta,head['direction']))/norm if norm else 0
        if abs(alignment)<.45:continue
        start,end=edge['source'],edge['target']
        if alignment<0:start,end=end,start
        candidate=dict(edge,source=start,target=end,direction='forward',bbox=head['bbox'],
            validation_status='pixel_direction_verified_endpoints_unverified',direction_verified=True,
            eligible_for_requirement_extraction=False,
            expert_approved=False)
        verified.append(dict(relation_index=index,candidate=candidate,pixel_evidence=head,
            original_relation=edge,scope='direction_of_visible_connector_only'))
        used.add(i)
    page['geometric_arrow_evidence']=verified
    return verified
