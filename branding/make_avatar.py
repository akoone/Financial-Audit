from PIL import Image, ImageDraw, ImageFilter
import math
S=2560; F=S/640
def p(*v): return [x*F for x in v]
img=Image.new("RGB",(S,S))
# diagonal gradient
top=(58,150,240); bot=(22,62,140)
px=img.load()
import numpy as np
y,x=np.mgrid[0:S,0:S]
g=Image.fromarray(((x+y)/(2*S)*255).astype("uint8"))
img=Image.composite(Image.new("RGB",(S,S),bot),Image.new("RGB",(S,S),top),g)
d=ImageDraw.Draw(img)
# soft glow circle
glow=Image.new("L",(S,S),0); ImageDraw.Draw(glow).ellipse(p(150,130,490,470),fill=60)
glow=glow.filter(ImageFilter.GaussianBlur(120*F/4))
img=Image.composite(Image.new("RGB",(S,S),(255,255,255)),img,glow)
d=ImageDraw.Draw(img)
# shadow layer for wallet
sh=Image.new("L",(S,S),0); sd=ImageDraw.Draw(sh)
sd.rounded_rectangle(p(150,250,470,470),radius=40*F,fill=110)
sh=sh.filter(ImageFilter.GaussianBlur(14*F))
img=Image.composite(Image.new("RGB",(S,S),(10,30,80)),img,sh.point(lambda v:v)); d=ImageDraw.Draw(img)
# bars behind wallet (rising)
bars=[(200,215,0),(262,175,0),(324,135,0)]
for x,y,_ in bars:
    d.rounded_rectangle(p(x,y,x+46,300),radius=10*F,fill=(255,255,255,));d.rounded_rectangle(p(x,y,x+46,300),radius=10*F,outline=(200,220,248),width=int(3*F))
# trend arrow
pts=[(170,175),(238,130),(290,148),(405,70)]
d.line([tuple(p(*q)) for q in pts],fill=(255,200,61),width=int(18*F),joint="curve")
for q in pts[:-1]: d.ellipse(p(q[0]-9,q[1]-9,q[0]+9,q[1]+9),fill=(255,200,61))
ax,ay=405,70; ang=math.atan2(70-148,405-290)
L=44;W=30
tip=(ax+12*math.cos(ang),ay+12*math.sin(ang))
b=(tip[0]-L*math.cos(ang),tip[1]-L*math.sin(ang))
l=(b[0]+W*math.sin(ang),b[1]-W*math.cos(ang)); r=(b[0]-W*math.sin(ang),b[1]+W*math.cos(ang))
d.polygon([tuple(p(*tip)),tuple(p(*l)),tuple(p(*r))],fill=(255,200,61))
# wallet body
d.rounded_rectangle(p(150,250,470,470),radius=40*F,fill=(255,255,255))
d.rounded_rectangle(p(150,250,470,300),radius=40*F,fill=(226,236,250))
d.rectangle(p(150,280,470,300),fill=(255,255,255))
# clasp
d.rounded_rectangle(p(370,330,490,400),radius=30*F,fill=(42,120,214))
d.ellipse(p(402,346,440,384),fill=(255,255,255))
# lines on wallet (ledger)
for i,y in enumerate((340,372,404)):
    d.rounded_rectangle(p(190,y,190+[140,110,150][i],y+14),radius=7*F,fill=(205,220,242))
# coin
cx,cy,R=230,440,58
d.ellipse(p(cx-R+6,cy-R+10,cx+R+6,cy+R+10),fill=(20,50,110))
d.ellipse(p(cx-R,cy-R,cx+R,cy+R),fill=(245,176,40))
d.ellipse(p(cx-R+10,cy-R+10,cx+R-10,cy+R-10),fill=(255,206,80))
# plus mark on coin (income/expense neutral: check mark)
d.line([tuple(p(cx-22,cy+2)),tuple(p(cx-6,cy+18)),tuple(p(cx+24,cy-16))],fill=(176,110,10),width=int(14*F),joint="curve")
img=img.resize((640,640),Image.LANCZOS)
img.save("app/static/bot-avatar.png")
