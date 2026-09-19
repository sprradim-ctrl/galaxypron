import os
import math
import random
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Lightweight, CPU-only "generator" that produces simple photos and short
# animated clips. No GPU needed. These are procedural compositions based on
# the user's text prompt, saved as PNG/JPG images and animated GIFs.
#
# NOTE: This is NOT diffusion/real generative AI. True AI image/video models
# need a CUDA GPU, which this machine (GT 710) does not have. This provides a
# real, local, CPU-powered generator that "makes simple images and videos".


class MediaGenerator:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def _timed_name(self, ext):
        return datetime.now().strftime('gen_%Y%m%d_%H%M%S_') + str(int(time.time() * 100))
    
    def _pick_palette(self, prompt):
        meta = []
        if any(k in prompt for k in ('sunset', 'sun', 'orange', 'red', 'fire')):
            meta.append(('sky_top', (255, 128, 30)))
            meta.append(('sky_bot', (255, 200, 80)))
            meta.append(('sun', (255, 240, 180)))
        elif any(k in prompt for k in ('night', 'moon', 'dark', 'star', 'space', 'galaxy')):
            meta.append(('sky_top', (10, 12, 48)))
            meta.append(('sky_bot', (30, 30, 90)))
            meta.append(('sun', (235, 235, 255)))
        elif any(k in prompt for k in ('ocean', 'sea', 'sea', 'water', 'blue')):
            meta.append(('sky_top', (120, 200, 255)))
            meta.append(('sky_bot', (230, 245, 255)))
            meta.append(('sun', (255, 255, 230)))
        elif any(k in prompt for k in ('forest', 'tree', 'green', 'mountain', 'hill')):
            meta.append(('sky_top', (140, 210, 255)))
            meta.append(('sky_bot', (220, 240, 255)))
            meta.append(('sun', (255, 250, 210)))
        else:
            meta.append(('sky_top', (110, 180, 240)))
            meta.append(('sky_bot', (215, 235, 250)))
            meta.append(('sun', (255, 250, 220)))
        return dict(meta)

    def _hill_row(self, draw, W, H, base_y, amp, col):
        points = [(0, H + 1)]
        steps = 20
        for i in range(steps + 1):
            x = (W * i) / steps
            y = base_y - amp * abs(math.sin(math.pi * (i / steps) * 1.7 + random.random() * 0.3))
            points.append((x, y))
        points.append((W, H + 1))
        draw.polygon(points, fill=col)

    def _draw_car_scene(self, dr, W, H, pal):
        """Draw a simple car on a road, with sky + ground + sun."""
        # sky gradient (simple vertical fill already done) + sun
        sunr = int(W * 0.07)
        dr.ellipse([W - 3 * sunr, int(H * 0.12), W - 2 * sunr, int(H * 0.12) + sunr], fill=pal['sun'])
        # ground / road
        road_y = int(H * 0.62)
        dr.rectangle([0, road_y, W, H], fill=(90, 95, 100))
        dr.rectangle([0, road_y, W, road_y + 4], fill=(150, 155, 160))
        # background trees/hills
        self._hill_row(dr, W, H, int(H * 0.66), int(H * 0.10), (70, 120, 70))

        # car body (centered)
        cw = int(W * 0.34)
        ch = int(cw * 0.32)
        cx = int(W * 0.33)
        cy = road_y - ch
        body = dr
        body.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=int(ch * 0.4), fill=(220, 60, 40))
        # cabin / roof
        roof_w = int(cw * 0.5)
        roof_h = int(ch * 0.55)
        roof_x = cx + int(cw * 0.18)
        body.rounded_rectangle([roof_x, cy - roof_h, roof_x + roof_w, cy + 2],
                               radius=int(roof_h * 0.5), fill=(60, 70, 85))
        # windows
        win_h = int(roof_h * 0.55)
        body.rectangle([roof_x + int(roof_w * 0.12), cy - roof_h + int(roof_h * 0.18),
                        roof_x + int(roof_w * 0.42), cy], fill=(170, 210, 235))
        body.rectangle([roof_x + int(roof_w * 0.56), cy - roof_h + int(roof_h * 0.18),
                        roof_x + roof_w - int(roof_w * 0.12), cy], fill=(170, 210, 235))
        # wheels
        wheel_r = int(ch * 0.5)
        wy = cy + ch - int(ch * 0.25)
        for wx in (cx + int(cw * 0.22), cx + cw - int(cw * 0.22)):
            dr.ellipse([wx - wheel_r, wy, wx + wheel_r, wy + 2 * wheel_r], fill=(30, 30, 32))
            dr.ellipse([wx - wheel_r // 2, wy + wheel_r // 2,
                        wx + wheel_r // 2, wy + 3 * wheel_r // 2], fill=(200, 200, 205))
        # headlights / taillights
        dr.ellipse([cx + cw - int(cw * 0.06), cy + int(ch * 0.3),
                    cx + cw + int(cw * 0.02), cy + int(ch * 0.45)], fill=(255, 230, 120))
        dr.ellipse([cx - int(cw * 0.02), cy + int(ch * 0.3),
                    cx + int(cw * 0.06), cy + int(ch * 0.45)], fill=(255, 90, 50))

    # ---------------------------------------------------------------- image
    def generate_image(self, prompt='landscape', width=1024, height=640):
        if width > 1600 or height > 1000:
            width, height = 1024, 640
        pal = self._pick_palette(prompt.lower())
        img = Image.new('RGB', (width, height), pal['sky_top'])
        dr = ImageDraw.Draw(img)

        # vertical gradient sky
        px = img.load()
        top, bot = pal['sky_top'], pal['sky_bot']
        for y in range(height):
            t = y / height
            r = int(top[0] + (bot[0] - top[0]) * t)
            g = int(top[1] + (bot[1] - top[1]) * t)
            b = int(top[2] + (bot[2] - top[2]) * t)
            for x in range(width):
                px[x, y] = (r, g, b)

        dr = ImageDraw.Draw(img)

        # ---- CAR / VEHICLE scene ----
        if any(k in prompt for k in ('car', 'truck', 'vehicle', 'auto', 'sports car', 'sedan')):
            self._draw_car_scene(dr, width, height, pal)
            img = img.filter(ImageFilter.SMOOTH_MORE)
            fname = self._timed_name('png') + '.png'
            path = self.output_dir / fname
            img.save(path, 'PNG')
            return {'file': fname, 'path': str(path), 'width': width, 'height': height,
                    'type': 'image', 'prompt': prompt, 'created': datetime.now().isoformat()}

        # sun / moon
        sunx = int(width * (0.2 + 0.6 * random.random()))
        suny = int(height * random.uniform(0.15, 0.35))
        rad = int(width * random.uniform(0.06, 0.09))
        dr.ellipse([sunx - rad, suny - rad, sunx + rad, suny + rad], fill=pal['sun'])

        # stars/sparkles for night
        if 'night' in prompt or 'star' in prompt or 'space' in prompt:
            for _ in range(180):
                x = random.randint(0, width - 1)
                y = random.randint(0, int(height * 0.5))
                c = random.randint(150, 255)
                dr.point((x, y), fill=(c, c, c))

        # layered hills/land
        colors = [
            (60, 110, 60), (90, 150, 80), (140, 185, 110),
        ]
        if 'snow' in prompt:
            colors = [(170, 190, 210), (200, 215, 230), (235, 240, 245)]
        elif 'ocean' in prompt or 'sea' in prompt:
            colors = [(30, 90, 150), (40, 120, 180), (70, 160, 210)]
        for i, col in enumerate(colors):
            base = height * (0.55 + i * 0.18) + i * 12
            self._hill_row(dr, width, height, int(base), height * random.uniform(0.12, 0.2), col)

        img = img.filter(ImageFilter.SMOOTH_MORE)
        fname = self._timed_name('png') + '.png'
        path = self.output_dir / fname
        img.save(path, 'PNG')
        return {'file': fname, 'path': str(path), 'width': width, 'height': height,
                'type': 'image', 'prompt': prompt, 'created': datetime.now().isoformat()}

    # ---------------------------------------------------------------- video
    def generate_video(self, prompt='sunset', seconds=3, size=480, fps=12):
        frames = max(3, int(seconds * fps))
        pal = self._pick_palette(prompt.lower())
        frames_list = []
        for f in range(frames):
            t = f / max(1, frames - 1)
            w = h = size
            img = Image.new('RGB', (w, h), pal['sky_top'])
            px = img.load()
            top, bot = pal['sky_top'], pal['sky_bot']
            for y in range(h):
                tt = y / h
                r = int(top[0] + (bot[0] - top[0]) * tt)
                g = int(top[1] + (bot[1] - top[1]) * tt)
                b = int(top[2] + (bot[2] - top[2]) * tt)
                for x in range(w):
                    px[x, y] = (r, g, b)
            dr = ImageDraw.Draw(img)

            # animated sun sliding across
            sunx = int(w * (0.1 + 0.8 * t))
            suny = int(h * (0.7 - 0.5 * t))
            rad = int(w * 0.07)
            dr.ellipse([sunx - rad, suny - rad, sunx + rad, suny + rad], fill=pal['sun'])

            # stars for night
            if 'night' in prompt or 'star' in prompt or 'space' in prompt:
                for _ in range(120):
                    x = random.randint(0, w - 1)
                    y = random.randint(0, int(h * 0.5))
                    c = random.randint(150, 255)
                    dr.point((x, y), fill=(c, c, c))

            colors = [(60, 110, 60), (90, 150, 80), (140, 185, 110)]
            if 'ocean' in prompt or 'sea' in prompt:
                colors = [(30, 90, 150), (40, 120, 180), (70, 160, 210)]
            elif 'snow' in prompt:
                colors = [(170, 190, 210), (200, 215, 230), (235, 240, 245)]
            for i, col in enumerate(colors):
                base = h * (0.55 + i * 0.18) + i * 12
                self._hill_row(dr, w, h, int(base), h * random.uniform(0.1, 0.18), col)

            frames_list.append(img.resize((size, size)))

        fname = self._timed_name('gif') + '.gif'
        path = self.output_dir / fname
        frames_list[0].save(path, 'GIF', save_all=True, append_images=frames_list[1:],
                            duration=1000 // fps, loop=0)
        return {'file': fname, 'path': str(path), 'frames': frames, 'seconds': seconds,
                'type': 'video', 'prompt': prompt, 'created': datetime.now().isoformat()}
