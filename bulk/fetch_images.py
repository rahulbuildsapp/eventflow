"""
Fetch real event photos for the bulk dataset, with a data budget.

generate.py COPYs image *paths* into events_eventimage but never writes
files, so every <img> on the event list 404s through Django's media
handler and the page crawls. This script downloads a *pool* of real photos
(concurrently, with threads) and hardlinks them to every path the database
references.

Why a pool: 6000 unique downloads would burn hundreds of MB for pixels
nobody inspects. A pool of ~120 photos reused across the dataset costs a
few MB of disk and a few MB of network traffic -- and every event still
gets a distinct-looking cover image (repeats only appear across pages).

Sources, in order of preference (auto):
  1. Pexels    -- needs PEXELS_API_KEY,    real event/concert photos
  2. Unsplash  -- needs UNSPLASH_ACCESS_KEY, real event/concert photos
  3. Picsum    -- keyless, real Unsplash-sourced photos (random subjects)

Usage
-----
    # auto source, 120-photo pool, 8 threads, ~5 MB budget
    bulk/venv/bin/python bulk/fetch_images.py

    bulk/venv/bin/python bulk/fetch_images.py \
        --source pexels --pool 200 --threads 12 --max-mb 20

    bulk/venv/bin/python bulk/fetch_images.py --source picsum
    bulk/venv/bin/python bulk/fetch_images.py --source local  # offline

Re-run after every --truncate reload; existing files are left untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import pathlib
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rsvp.settings_bulk")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

import requests  # noqa: E402

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

WIDTH, HEIGHT = 800, 450
UA = {"User-Agent": "eventflow-bulk-loader/1.0"}
PEXELS_QUERIES = [
    "music festival crowd", "concert stage lights", "conference audience",
    "sports stadium fans", "art exhibition gallery", "food festival",
    "night market lights", "theater performance",
]
UNSPLASH_QUERIES = ["event", "concert", "festival", "conference"]
PALETTE = [
    (36, 41, 51), (43, 74, 117), (29, 78, 74), (88, 60, 35),
    (94, 39, 61), (60, 47, 94), (34, 63, 87), (47, 82, 47),
]


class Budget:
    """Thread-safe download budget in megabytes."""

    def __init__(self, max_mb: float):
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.used = 0
        self.lock = threading.Lock()
        self.exhausted = False

    def take(self, n: int) -> bool:
        with self.lock:
            if self.exhausted or self.used + n > self.max_bytes:
                self.exhausted = True
                return False
            self.used += n
            return True


def normalize(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    scale = max(WIDTH / img.width, HEIGHT / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)),
                     Image.LANCZOS)
    left = (img.width - WIDTH) // 2
    top = (img.height - HEIGHT) // 2
    img = img.crop((left, top, left + WIDTH, top + HEIGHT))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=72, optimize=True)
    return out.getvalue()


def download(url: str, budget: Budget, timeout: int = 20) -> bytes | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout, stream=True)
        r.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if not budget.take(len(chunk)):
                return None
            chunks.append(chunk)
        return normalize(b"".join(chunks))
    except Exception:
        return None


# ---------------------------------------------------------------- sources

def pool_pexels(want: int, budget: Budget, threads: int) -> list[bytes]:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        sys.exit("PEXELS_API_KEY not set")
    urls: list[str] = []
    hdr = {"Authorization": key}
    for query in PEXELS_QUERIES:
        if len(urls) >= want:
            break
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": query, "per_page": 80},
                         headers=hdr, timeout=20)
        r.raise_for_status()
        for photo in r.json().get("photos", []):
            src = photo.get("src") or {}
            u = src.get("large2x") or src.get("large") or src.get("original")
            if u:
                urls.append(u)
    return gather(urls[:want], budget, threads)


def pool_unsplash(want: int, budget: Budget, threads: int) -> list[bytes]:
    key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not key:
        sys.exit("UNSPLASH_ACCESS_KEY not set")
    urls: list[str] = []
    for query in UNSPLASH_QUERIES:
        if len(urls) >= want:
            break
        r = requests.get("https://api.unsplash.com/search/photos",
                         params={"query": query, "per_page": 30},
                         headers={"Authorization": f"Client-ID {key}"},
                         timeout=20)
        r.raise_for_status()
        for res in r.json().get("results", []):
            u = (res.get("urls") or {}).get("regular")
            if u:
                urls.append(u)
    return gather(urls[:want], budget, threads)


def pool_picsum(want: int, budget: Budget, threads: int) -> list[bytes]:
    # Keyless: picsum serves curated Unsplash photos by seed.
    urls = [f"https://picsum.photos/seed/{'efk%04d' % i}/{WIDTH * 2}/{HEIGHT * 2}"
            for i in range(want * 2)]  # over-fetch; seeds sometimes 404
    return gather(urls, budget, threads, want=want)


def pool_local(want: int, budget: Budget, threads: int) -> list[bytes]:
    out = []
    for i in range(want):
        top = PALETTE[i % len(PALETTE)]
        bottom = tuple(min(255, c + 60) for c in top)
        img = Image.new("RGB", (WIDTH, HEIGHT))
        d = ImageDraw.Draw(img)
        for y in range(HEIGHT):
            t = y / (HEIGHT - 1)
            d.line([(0, y), (WIDTH, y)],
                   fill=tuple(int(top[c] + (bottom[c] - top[c]) * t)
                              for c in range(3)))
        d.rectangle([20, 20, WIDTH - 20, HEIGHT - 20],
                    outline=(255, 255, 255), width=3)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70, optimize=True)
        out.append(buf.getvalue())
    return out


def gather(urls: list[str], budget: Budget, threads: int,
           want: int | None = None) -> list[bytes]:
    imgs: list[bytes] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(download, u, budget) for u in urls]
        for fut in as_completed(futures):
            data = fut.result()
            if data:
                with lock:
                    imgs.append(data)
            if want and len(imgs) >= want:
                for f in futures:
                    f.cancel()
                break
    return imgs


# ---------------------------------------------------------------- materialise

def link(target: pathlib.Path, source: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        return
    try:
        os.link(source, target)
    except OSError:  # cross-filesystem fallback
        target.write_bytes(source.read_bytes())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", choices=["auto", "pexels", "unsplash",
                                        "picsum", "local"], default="auto")
    p.add_argument("--pool", type=int, default=120,
                   help="number of unique photos to download (default 120)")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--max-mb", type=float, default=8.0,
                   help="hard network budget in MB (default 8)")
    p.add_argument("--reuse", action="store_true",
                   help="skip download, use the photo pool already on disk")
    args = p.parse_args()

    media_root = pathlib.Path(settings.MEDIA_ROOT)
    pool_dir = media_root / "_pool"

    if args.reuse:
        files = sorted(pool_dir.glob("photo_*.jpg"))
        if not files:
            sys.exit(f"no pool in {pool_dir}; run without --reuse first")
        print(f"reuse: {len(files)} photos already on disk")
        args.source, budget_used = "reuse", 0
    else:
        source = args.source
        if source == "auto":
            if os.environ.get("PEXELS_API_KEY"):
                source = "pexels"
            elif os.environ.get("UNSPLASH_ACCESS_KEY"):
                source = "unsplash"
            else:
                source = "picsum"
                print("no PEXELS_API_KEY / UNSPLASH_ACCESS_KEY found -> picsum")

        budget = Budget(args.max_mb)
        fetchers = {"pexels": pool_pexels, "unsplash": pool_unsplash,
                    "picsum": pool_picsum, "local": pool_local}
        print(f"source={source} pool={args.pool} threads={args.threads} "
              f"budget={args.max_mb}MB")
        imgs = fetchers[source](args.pool, budget, args.threads)
        if not imgs:
            print("nothing downloaded, falling back to generated placeholders")
            imgs = pool_local(args.pool, budget, args.threads)

        pool_dir.mkdir(parents=True, exist_ok=True)
        for old in pool_dir.glob("photo_*.jpg"):
            old.unlink()
        files = []
        for i, data in enumerate(imgs):
            f = pool_dir / f"photo_{i:04d}.jpg"
            f.write_bytes(data)
            files.append(f)
        budget_used = budget.used
        print(f"pool: {len(files)} photos, "
              f"network used {budget_used / 1024 / 1024:.1f}MB, "
              f"on disk {sum(f.stat().st_size for f in files) / 1024 / 1024:.1f}MB")

    from events.models import EventImage

    made = skipped = 0
    for path in EventImage.objects.values_list("image", flat=True):
        pth = media_root / str(path)
        if pth.exists():
            skipped += 1
            continue
        h = int(hashlib.sha1(str(path).encode()).hexdigest(), 16)
        link(pth, files[h % len(files)])
        made += 1

    print(f"linked into place: {made} (already present: {skipped})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
