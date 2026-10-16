from pathlib import Path
from io import BytesIO
import json, resvg_py, struct, shutil, zipfile
from PIL import Image, ImageCms

ROOT=Path(__file__).resolve().parent.parent
SRC=ROOT/'assets'/'brand-approved'/'micast.svg'
OUT=ROOT/'assets'/'icons'
OUT.mkdir(parents=True,exist_ok=True)
ICC=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
svg=SRC.read_text(encoding='utf-8')
win_svg=svg

def render(size, source=svg, shell=False):
    # Area averaging avoids Lanczos ringing (isolated alpha=1 pixels outside
    # the tile become opaque specks in Windows' legacy 1-bit icon mask).
    im = Image.open(BytesIO(resvg_py.svg_to_bytes(svg_string=source,width=size*4,height=size*4))).convert('RGBA').resize((size,size),Image.Resampling.BOX)
    pixels=list(im.getdata())
    if shell and size <= 48:
        # Small shell images may travel through a binary-mask image list.
        # Snap only the outer alpha coverage; internal artwork stays antialiased.
        pixels=[(0,0,0,0) if a<128 else (r,g,b,255) for r,g,b,a in pixels]
    im.putdata([(0,0,0,0) if a == 0 else (r,g,b,a) for r,g,b,a in pixels])
    return im
def save(name,size,source=svg):
    p=OUT/name; p.parent.mkdir(parents=True,exist_ok=True); render(size,source,shell=name.startswith('windows/')).save(p,icc_profile=ICC,optimize=True); return p
def ico(name,sizes,source=svg):
    # Explicit BGRA DIB + AND mask supports both alpha-aware and legacy shell
    # consumers. Render every size from SVG; never resize another ICO frame.
    chunks=[]
    for size in sizes:
        im=render(size,source,shell=name.startswith('windows/'))
        stride=((size+31)//32)*4
        mask=bytearray(stride*size)
        for y in range(size):
            for x in range(size):
                if im.getpixel((x,y))[3] == 0:
                    mask[(size-1-y)*stride+x//8] |= 128 >> (x%8)
        pixels=im.tobytes('raw','BGRA',0,-1)
        header=struct.pack('<IiiHHIIiiII',40,size,size*2,1,32,0,len(pixels)+len(mask),0,0,0,0)
        chunks.append(header+pixels+mask)
    offset=6+16*len(sizes)
    data=struct.pack('<HHH',0,1,len(sizes))
    for size,chunk in zip(sizes,chunks):
        data+=struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(chunk),offset)
        offset+=len(chunk)
    p=OUT/name; p.write_bytes(data+b''.join(chunks)); return p

# fnOS: use the same 90% tile footprint as MiCast Go, retaining the existing
# speaker artwork proportions inside the tile.
fnos=svg.replace('x="5" y="5" width="90" height="90" rx="22"', 'x="1.5" y="1.5" width="97" height="97" rx="23.7"')
fnos=fnos.replace('transform="translate(-3 -4.5) scale(1.08)"', 'transform="translate(50 50) scale(1.20) translate(-50 -50) translate(-3 -4.5) scale(1.08)"')
fnos=fnos.replace('stroke-width="2.6"','stroke-width="3.05"')
fnos=fnos.replace('viewBox="0 0 100 100">', 'viewBox="0 0 100 100"><g transform="translate(50 50) scale(0.9278350515463918) translate(-50 -50)">').replace('</svg>', '</g></svg>')
fnos_small=fnos.replace('stroke-width="3.05"','stroke-width="3.25"')
save('fnos/ICON.PNG',64,fnos_small); save('fnos/ICON_256.PNG',256,fnos)
(OUT/'fnos/micast.svg').write_text(fnos,encoding='utf-8')
# Windows shell/app and installer
for s in [16,20,24,32,40,48,64,96,128,256]: save(f'windows/micast-{s}.png',s,win_svg)
ico('windows/micast.ico',[16,20,24,32,40,48,64,96,128,256],win_svg); ico('windows/setup.ico',[16,24,32,48,256],win_svg)
# Tray: simplified glyph on transparent canvas, light and dark taskbar variants
tray_svg=svg.replace('fill="#B94B36"','fill="none"').replace('fill="#FFF7ED"','fill="#17212F"')
tray_dark=tray_svg.replace('#17212F','#FFFFFF')
for theme,src in [('light',tray_svg),('dark',tray_dark)]:
    for s in [16,20,24,32,40,48,64]: save(f'tray/tray-{theme}-{s}.png',s,src)
    ico(f'tray/tray-{theme}.ico',[16,20,24,32,40,48,64],src)
# Web
for s in [16,32,48,64,96,128,192,256,512]: save(f'web/icon-{s}.png',s)
# Browser tabs have only ~16 CSS pixels: use the whole tile and enlarge the
# approved speaker group while retaining the regular mark for in-page branding.
favicon=svg.replace('x="5" y="5" width="90" height="90" rx="22"', 'x="0" y="0" width="100" height="100" rx="24"')
favicon=favicon.replace('transform="translate(-3 -4.5) scale(1.08)"', 'transform="translate(50 50) scale(1.28) translate(-50 -50) translate(-3 -4.5) scale(1.08)"')
favicon=favicon.replace('stroke-width="2.6"','stroke-width="3.2"')
ico('web/favicon.ico',[16,32,48],favicon)
(OUT/'web/favicon.svg').write_text(favicon,encoding='utf-8')
for s in [16,32,48]: save(f'web/favicon-{s}.png',s,favicon)
(OUT/'web/micast.svg').write_text(svg,encoding='utf-8')
mask=svg.replace('<rect x="5" y="5" width="90" height="90" rx="22" fill="#B94B36"/>','<rect width="100" height="100" fill="#B94B36"/>')
touch=render(180,mask).convert('RGB')
touch.save(OUT/'web/apple-touch-icon.png',icc_profile=ICC,optimize=True)
for s in [192,512]: save(f'web/maskable-{s}.png',s,mask)
icons=[{'src':f'icon-{s}.png','sizes':f'{s}x{s}','type':'image/png','purpose':'any'} for s in [192,512]]
icons += [{'src':f'maskable-{s}.png','sizes':f'{s}x{s}','type':'image/png','purpose':'maskable'} for s in [192,512]]
(OUT/'web/manifest-icons.json').write_text(json.dumps({'icons':icons},indent=2),encoding='utf-8')
# validation
report=[]
for p in OUT.rglob('*.png'):
    im=Image.open(p); assert im.info.get('icc_profile')==ICC; report.append({'file':p.relative_to(OUT).as_posix(),'size':im.size,'bytes':p.stat().st_size})
for p in [OUT/'fnos/ICON.PNG',OUT/'fnos/ICON_256.PNG']: assert p.stat().st_size<=1024*1024
(OUT/'validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(f'generated {len(report)} PNGs in {OUT}')
# Keep the deployed public files and distributable archive in sync.
public=ROOT/'web/public/icons'
public.mkdir(parents=True,exist_ok=True)
for p in (OUT/'web').iterdir():
    if p.is_file(): shutil.copy2(p,public/p.name)
# Both fnOS packaging scripts consume these public paths.
shutil.copy2(OUT/'fnos/ICON.PNG',public/'fnos-64.png')
shutil.copy2(OUT/'fnos/ICON_256.PNG',public/'fnos-256.png')
with zipfile.ZipFile(ROOT/'assets/micast-icons.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for p in OUT.rglob('*'):
        if p.is_file(): archive.write(p,p.relative_to(OUT))
