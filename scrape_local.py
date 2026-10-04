"""
Tap.az-dan elanları LOKAL (öz kompüter / telefon IP-n) çəkib items.json faylına yazır.
Sonra bu faylı veb tətbiqdə "JSON yüklə" bölməsindən yüklə.

  pip install requests beautifulsoup4
  python scrape_local.py                  # sürətli (vəziyyət "?" qalır)
  python scrape_local.py --details        # tanınan elanların Yeni/İşlənmiş vəziyyətini də oxuyur (yavaş)
  python scrape_local.py --pages 3 --out items.json

Android-da: Termux -> pkg install python -> pip install requests beautifulsoup4
"""
import argparse
import sys
import time

import core

ap = argparse.ArgumentParser()
ap.add_argument("--pages", type=int, default=2)
ap.add_argument("--delay", type=float, default=1.0)
ap.add_argument("--out", default="items.json")
ap.add_argument("--details", action="store_true")
ap.add_argument("--proxy", default=None)
a = ap.parse_args()

items, errors = core.scrape(core.DEFAULT_QUERIES, a.pages, a.delay,
                            progress=lambda f: print(f"\r{f:.0%}", end="", file=sys.stderr),
                            proxy=a.proxy)
print(file=sys.stderr)
for e in errors:
    print("!", e, file=sys.stderr)
for it in items:
    core.classify(it)
good = [i for i in items if i.cat]
print(f"{len(items)} elan, {len(good)} tanındı", file=sys.stderr)

if a.details:
    for n, it in enumerate(good, 1):
        d = core.fetch_details(it.url, a.proxy)
        it.condition = d["condition"]
        it.image = it.image or d["image"]
        print(f"\rdetallar {n}/{len(good)}", end="", file=sys.stderr)
        time.sleep(a.delay)
    print(file=sys.stderr)

with open(a.out, "w", encoding="utf-8") as f:
    f.write(core.items_to_json(good))
print(f"Yazıldı: {a.out}")
