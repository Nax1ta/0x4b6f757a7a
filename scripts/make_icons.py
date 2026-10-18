"""Convert an already transparent local PNG into app icon resources."""
import argparse
from pathlib import Path
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path)
args = parser.parse_args()
source = Image.open(args.source).convert('RGBA')
if source.getextrema()[3][0] != 0:
    raise ValueError('The input needs a genuinely transparent background')
source = source.crop(source.getbbox())
root = Path(__file__).resolve().parents[1] / '.local/icons'
root.mkdir(parents=True, exist_ok=True)


def canvas(size, extent):
    image = source.copy()
    image.thumbnail((extent, extent), Image.Resampling.LANCZOS)
    output = Image.new('RGBA', (size, size))
    output.alpha_composite(image, ((size-image.width)//2, (size-image.height)//2))
    return output


master = canvas(1024, 960)
master.save(root / 'icon.png')
master.save(root / 'app.ico', format='ICO', sizes=[(n,n) for n in (16,20,24,32,40,48,64,96,128,256)])
resources = root / 'android'
for density, size in [('mdpi',48), ('hdpi',72), ('xhdpi',96), ('xxhdpi',144), ('xxxhdpi',192)]:
    directory = resources / ('mipmap-' + density)
    directory.mkdir(parents=True, exist_ok=True)
    canvas(size, round(size * .94)).save(directory / 'ic_launcher.png')
foreground = resources / 'drawable-nodpi'
foreground.mkdir(parents=True, exist_ok=True)
# Fit the complete portrait (including its inset picture) inside round masks.
canvas(432, 208).save(foreground / 'icon_foreground.png')
adaptive = resources / 'mipmap-anydpi-v26'
adaptive.mkdir(parents=True, exist_ok=True)
(adaptive / 'ic_launcher.xml').write_text('''<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@android:color/transparent" />
    <foreground android:drawable="@drawable/icon_foreground" />
</adaptive-icon>
''', encoding='utf-8')
print('Icon resources generated in', root)
