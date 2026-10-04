#!/usr/bin/env python3
"""
Tap.az PC Builder
-----------------
1) Tap.az "Kompüter hissələri" elanlarını toplayır (ad, qiymət, vəziyyət, link)
2) CPU/Ana plata soketi, RAM növü (DDR4/DDR5), PSU gücü uyğunluğunu yoxlayır
3) Büdcəni komponentlər arasında bölür, ən yaxşı performanslı konfiqurasiyanı seçir
4) Nəticəni cədvəl şəklində çıxarır (link + qiymət + yekun)

Quraşdırma:  pip install requests beautifulsoup4
İstifadə:
  python tapaz_pc_builder.py --budget 1500 --pages 5
  python tapaz_pc_builder.py --budget 1500 --save-json items.json     # scrape + saxla
  python tapaz_pc_builder.py --budget 1500 --from-json items.json     # offline işlət
  python tapaz_pc_builder.py --budget 1500 --details                  # final hissələr üçün "Vəziyyət" səhifədən oxunur

Qeyd: Tap.az-ın HTML strukturu dəyişə bilər. Seçicilər işləməsə, parse_listing()
funksiyasını yoxla. Sorğular arasında gecikmə qoyulub - sayta yük salma və
saytın istifadə şərtlərinə riayət et.
"""
import argparse
import itertools
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin

BASE = "https://tap.az"
# Kateqoriya linkini brauzerdən yoxla və lazım olsa --url ilə dəyiş
DEFAULT_URL = "https://tap.az/elanlar/elektronika/komputer-hisseleri"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "az,en;q=0.8",
}

# ----------------------------------------------------------------------------
# Məlumat bazası (təxmini nisbi performans balı və TDP). Öz məlumatını əlavə et.
# ----------------------------------------------------------------------------
GPU_DB = {  # ad: (performans, TDP_W)
    "gtx 1050 ti": (65, 75), "gtx 1650 super": (115, 100), "gtx 1650": (100, 75),
    "gtx 1660 super": (150, 125), "gtx 1660 ti": (155, 120), "gtx 1660": (140, 120),
    "rx 570": (95, 150), "rx 580": (125, 185), "rx 5600 xt": (175, 150),
    "rtx 2060 super": (215, 175), "rtx 2060": (190, 160), "rtx 2070": (235, 175),
    "rtx 3050": (165, 130), "rtx 3060 ti": (280, 200), "rtx 3060": (230, 170),
    "rx 6600 xt": (265, 160), "rx 6600": (235, 132), "rx 6700 xt": (330, 230),
    "rx 6800": (420, 250), "rtx 3070 ti": (350, 290), "rtx 3070": (320, 220),
    "rtx 3080": (430, 320), "rtx 4060 ti": (330, 160), "rtx 4060": (270, 115),
    "rtx 4070 super": (460, 220), "rtx 4070": (400, 200), "rx 7600": (260, 165),
    "rx 7700 xt": (390, 245), "rx 7800 xt": (450, 263), "rtx 5060": (320, 145),
}
CPU_DB = {
    # AM4
    "3600": (100, 65), "5500": (115, 65), "5600x": (140, 65), "5600": (135, 65),
    "5700x": (165, 65), "5800x3d": (200, 105), "5800x": (170, 105),
    # AM5
    "7600x": (195, 105), "7600": (190, 65), "7700x": (225, 105), "7700": (220, 65),
    "7800x3d": (280, 120), "9600x": (215, 65), "9700x": (245, 65),
    # LGA1200 / 1151
    "9400f": (90, 65), "9400": (90, 65), "10400f": (105, 65), "10400": (105, 65),
    "11400f": (125, 65), "11400": (125, 65),
    # LGA1700
    "12100f": (110, 58), "12100": (110, 60), "12400f": (140, 65), "12400": (140, 65),
    "12600kf": (190, 125), "12600k": (190, 125), "13400f": (160, 65), "13400": (160, 65),
    "13600kf": (250, 125), "13600k": (250, 125), "14400f": (165, 65), "14400": (165, 65),
    "14600kf": (260, 125), "14600k": (260, 125),
}
# Chipset -> soket
CHIPSET_SOCKET = {
    **{c: "AM4" for c in ["a320", "b350", "x370", "b450", "x470", "a520", "b550", "x570"]},
    **{c: "AM5" for c in ["a620", "b650", "x670", "b840", "b850", "x870"]},
    **{c: "LGA1700" for c in ["h610", "b660", "h670", "z690", "b760", "h770", "z790"]},
    **{c: "LGA1200" for c in ["h410", "b460", "h470", "z490", "h510", "b560", "h570", "z590"]},
    **{c: "LGA1151" for c in ["h310", "b360", "b365", "h370", "z370", "z390",
                              "h110", "b150", "b250", "h170", "z170", "z270"]},
}
SOCKET_DEFAULT_DDR = {"AM4": "DDR4", "AM5": "DDR5", "LGA1200": "DDR4", "LGA1151": "DDR4"}

# Büdcə bölgüsü (oyun PC-si)
SPLIT = {"gpu": 0.42, "cpu": 0.22, "mb": 0.12, "ram": 0.09, "psu": 0.08, "ssd": 0.07}
CAP_FACTOR = 1.6        # kateqoriya limiti = pay * CAP_FACTOR (balans üçün çevik)
PSU_HEADROOM = 1.3      # (CPU+GPU TDP + 100W) * 1.3 <= PSU gücü
TOP_N = 8


# ----------------------------------------------------------------------------
@dataclass
class Item:
    title: str
    price: float
    url: str
    condition: str = "?"
    cat: Optional[str] = None
    spec: dict = field(default_factory=dict)

    @property
    def perf(self):
        return self.spec.get("perf", 0)


def norm(t: str) -> str:
    return re.sub(r"[\-_/,]+", " ", t.lower())


# ----------------------------------------------------------------------------
# 1) SCRAPING
# ----------------------------------------------------------------------------
def parse_price(text: str) -> Optional[float]:
    m = re.search(r"(\d[\d\s.,]*)\s*(azn|₼|man)?", text.lower())
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    return float(digits) if digits else None


def parse_listing(html: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    cards = soup.select("div.products-i, div.products-link, article, div.product")
    for c in cards:
        a = c if c.name == "a" else c.find("a", href=re.compile(r"/elanlar/.*\d+"))
        if not a or not a.get("href"):
            continue
        name_el = c.select_one(".products-name, .product-name, .title, h3")
        price_el = c.select_one(".product-price, .price, .products-price, [class*=price]")
        title = (name_el or a).get_text(" ", strip=True)
        price = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
        if not title or price is None:
            continue
        out.append(Item(title=title, price=price, url=urljoin(BASE, a["href"])))
    return out


def fetch_condition(session, url: str) -> str:
    """Elan səhifəsindən 'Vəziyyəti: Yeni/İşlənmiş' oxu."""
    from bs4 import BeautifulSoup
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        label = soup.find(string=re.compile(r"Vəziyyət", re.I))
        if label:
            cell = label.find_parent(["td", "dt", "span", "div"])
            nxt = cell.find_next_sibling() if cell else None
            if nxt:
                return nxt.get_text(" ", strip=True)
        text = soup.get_text(" ", strip=True).lower()
        if "işlənmiş" in text or "ikinci əl" in text:
            return "İşlənmiş"
        if "yeni" in text:
            return "Yeni"
    except Exception as e:  # noqa
        print(f"  ! vəziyyət oxunmadı: {e}", file=sys.stderr)
    return "?"


def scrape(url: str, pages: int, delay: float):
    import requests
    s = requests.Session()
    seen, items = set(), []
    for p in range(1, pages + 1):
        u = f"{url}{'&' if '?' in url else '?'}page={p}"
        print(f"[scrape] {u}", file=sys.stderr)
        r = s.get(u, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  status {r.status_code}, dayandı", file=sys.stderr)
            break
        new = [i for i in parse_listing(r.text) if i.url not in seen]
        if not new:
            break
        for i in new:
            seen.add(i.url)
        items += new
        time.sleep(delay)
    return items, s


# ----------------------------------------------------------------------------
# 2) KLASSİFİKASİYA və SPEC çıxarışı
# ----------------------------------------------------------------------------
def find_db(db: dict, t: str):
    for key in sorted(db, key=len, reverse=True):
        if re.search(rf"(?<![\w]){re.escape(key)}(?![\d])", t):
            return key, db[key]
    return None, None


def cpu_socket(t: str):
    m = re.search(r"ryzen\s*[3579]\s*(\d{4})", t)
    if m:
        return "AM5" if m.group(1)[0] in "789" else "AM4"
    m = re.search(r"i[3579]\s*(\d{4,5})", t)
    if m:
        n = m.group(1)
        gen = int(n[:2]) if len(n) == 5 else int(n[0])
        if gen >= 12:
            return "LGA1700"
        if gen in (10, 11):
            return "LGA1200"
        return "LGA1151"
    return None


def board_chipset(t: str):
    for chip in CHIPSET_SOCKET:
        if re.search(rf"(?<![a-z0-9]){chip}(?![0-9])", t):
            return chip
    return None


def ram_info(t: str):
    ddr = re.search(r"ddr\s*([345])", t)
    if not ddr:
        return None
    kit = re.search(r"(\d)\s*x\s*(\d{1,3})\s*gb", t)
    if kit:
        gb = int(kit.group(1)) * int(kit.group(2))
    else:
        g = re.search(r"(\d{1,3})\s*gb", t)
        if not g:
            return None
        gb = int(g.group(1))
    return {"ddr": f"DDR{ddr.group(1)}", "gb": gb}


def ssd_info(t: str):
    if not re.search(r"ssd|nvme|m\.?2", t):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)", t)
    if not m:
        return None
    gb = float(m.group(1)) * (1000 if m.group(2) == "tb" else 1)
    return {"gb": int(gb)}


def psu_info(t: str):
    if not re.search(r"psu|qida\s*blok|power\s*supply|блок питания|\bpowerx|atx", t):
        return None
    m = re.search(r"(\d{3,4})\s*(w|watt|vt)\b", t)
    return {"watt": int(m.group(1))} if m else None


def classify(it: Item):
    t = norm(it.title)
    if re.search(r"notebook|laptop|noutbuk|sodimm|so-dimm|ikinci|komplekt|kompüter dəsti", t) \
            and "sodimm" in t.replace("-", ""):
        return
    hits = {}
    gk, gv = find_db(GPU_DB, t)
    if gk and re.search(r"rtx|gtx|rx|radeon|geforce|videokart|video kart", t):
        hits["gpu"] = {"perf": gv[0], "tdp": gv[1], "model": gk}
    chip = board_chipset(t)
    if chip and re.search(r"ana plata|motherboard|mainboard|plata|mb\b|asus|msi|gigabyte|asrock", t):
        sock = CHIPSET_SOCKET[chip]
        ddr = re.search(r"ddr\s*([45])", t)
        hits["mb"] = {"socket": sock, "chip": chip,
                      "ddr": f"DDR{ddr.group(1)}" if ddr else SOCKET_DEFAULT_DDR.get(sock)}
    sock = cpu_socket(t)
    if sock and not chip:
        ck, cv = find_db(CPU_DB, t)
        if ck:
            hits["cpu"] = {"socket": sock, "perf": cv[0], "tdp": cv[1], "model": ck}
    if not chip and not gk:
        r = ram_info(t)
        s = ssd_info(t)
        p = psu_info(t)
        if r and not s:
            hits["ram"] = r
        elif s:
            hits["ssd"] = s
        elif p:
            hits["psu"] = p
    if len(hits) == 1:
        it.cat, it.spec = next(iter(hits.items()))
        if it.cat == "ram":
            it.spec["perf"] = it.spec["gb"]


# ----------------------------------------------------------------------------
# 3) UYĞUNLUQ
# ----------------------------------------------------------------------------
def compatible(cpu, mb, ram, psu, gpu):
    """(ok, xəbərdarlıqlar)"""
    warns = []
    if cpu.spec["socket"] != mb.spec["socket"]:
        return False, []
    mb_ddr = mb.spec.get("ddr")
    if mb_ddr and mb_ddr != ram.spec["ddr"]:
        return False, []
    if not mb_ddr:
        warns.append("Ana platanın DDR növü başlıqdan bilinmir - yoxla")
    need = (cpu.spec["tdp"] + gpu.spec["tdp"] + 100) * PSU_HEADROOM
    if psu.spec["watt"] < need:
        return False, []
    return True, warns


def required_psu(cpu, gpu):
    return int((cpu.spec["tdp"] + gpu.spec["tdp"] + 100) * PSU_HEADROOM)


# ----------------------------------------------------------------------------
# 4) OPTİMİZASİYA
# ----------------------------------------------------------------------------
def shortlist(items, cat, budget):
    cap = budget * SPLIT[cat] * CAP_FACTOR
    pool = [i for i in items if i.cat == cat and 0 < i.price <= cap]
    if cat == "psu":
        pool = [i for i in pool if i.spec["watt"] >= 400]
    by_perf = sorted(pool, key=lambda i: -(i.perf or i.spec.get("watt", 0)))[:TOP_N]
    by_val = sorted(pool, key=lambda i: -((i.perf or i.spec.get("watt", 0)) / i.price))[:TOP_N]
    res, seen = [], set()
    for i in by_perf + by_val:
        if i.url not in seen:
            seen.add(i.url)
            res.append(i)
    return res


def score(gpu, cpu, ram, ssd):
    return (0.55 * gpu.perf + 0.30 * cpu.perf
            + 1.0 * min(ram.spec["gb"], 32) + 0.02 * min(ssd.spec["gb"], 1000))


def optimize(items, budget):
    sl = {c: shortlist(items, c, budget) for c in SPLIT}
    for c, v in sl.items():
        print(f"[opt] {c}: {len(v)} namizəd", file=sys.stderr)
        if not v:
            return None, f"'{c}' kateqoriyası üçün büdcəyə uyğun elan tapılmadı"
    best, best_key = None, None
    for gpu, cpu in itertools.product(sl["gpu"], sl["cpu"]):
        if gpu.price + cpu.price > budget:
            continue
        for mb in sl["mb"]:
            if mb.spec["socket"] != cpu.spec["socket"]:
                continue
            base = gpu.price + cpu.price + mb.price
            for ram in sl["ram"]:
                if mb.spec.get("ddr") and mb.spec["ddr"] != ram.spec["ddr"]:
                    continue
                for psu in sl["psu"]:
                    for ssd in sl["ssd"]:
                        total = base + ram.price + psu.price + ssd.price
                        if total > budget:
                            continue
                        ok, warns = compatible(cpu, mb, ram, psu, gpu)
                        if not ok:
                            continue
                        key = (score(gpu, cpu, ram, ssd), -total)
                        if best_key is None or key > best_key:
                            best_key = key
                            best = (dict(gpu=gpu, cpu=cpu, mb=mb, ram=ram, psu=psu, ssd=ssd), warns)
    if not best:
        return None, "Uyğun və büdcəyə sığan kombinasiya tapılmadı (büdcəni artır və ya daha çox səhifə yüklə)"
    return best, None


# ----------------------------------------------------------------------------
# 5) ÇIXIŞ
# ----------------------------------------------------------------------------
LABELS = {"cpu": "Prosessor", "mb": "Ana plata", "gpu": "Videokart",
          "ram": "RAM", "psu": "Qida bloku", "ssd": "SSD"}


def render(build, warns, budget):
    rows = [("Hissə", "Ad", "Vəziyyət", "Qiymət (AZN)", "Link")]
    total = 0
    for c in ["cpu", "mb", "gpu", "ram", "psu", "ssd"]:
        i = build[c]
        total += i.price
        rows.append((LABELS[c], i.title[:60], i.condition, f"{i.price:.0f}", i.url))
    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    md.append(f"| **YEKUN** | | | **{total:.0f}** | Büdcə: {budget:.0f} AZN, qalıq: {budget - total:.0f} |")
    out = "\n".join(md)

    cpu, gpu, mb, psu = build["cpu"], build["gpu"], build["mb"], build["psu"]
    out += "\n\nUyğunluq yoxlanışı:"
    out += f"\n  ✔ Soket: CPU {cpu.spec['socket']} = Ana plata {mb.spec['socket']}"
    out += f"\n  ✔ RAM: {build['ram'].spec['ddr']} (ana plata: {mb.spec.get('ddr') or 'bilinmir'})"
    out += (f"\n  ✔ PSU: {psu.spec['watt']}W ≥ tələb olunan ~{required_psu(cpu, gpu)}W "
            f"(CPU {cpu.spec['tdp']}W + GPU {gpu.spec['tdp']}W + 100W, ×{PSU_HEADROOM})")
    for w in warns:
        out += f"\n  ⚠ {w}"
    out += "\n\nQeyd: korpus, soyutma və s. büdcəyə daxil deyil. Alışdan əvvəl satıcıdan sınaq/zəmanət tələb et."
    return out


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, required=True, help="Ümumi büdcə (AZN)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--details", action="store_true", help="Final hissələrin vəziyyətini səhifədən oxu")
    ap.add_argument("--save-json")
    ap.add_argument("--from-json")
    a = ap.parse_args()

    session = None
    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            items = [Item(**d) for d in json.load(f)]
    else:
        items, session = scrape(a.url, a.pages, a.delay)
    print(f"[info] {len(items)} elan", file=sys.stderr)

    for it in items:
        if not it.cat:
            classify(it)
    if a.save_json:
        with open(a.save_json, "w", encoding="utf-8") as f:
            json.dump([asdict(i) for i in items], f, ensure_ascii=False, indent=1)
    cnt = {}
    for i in items:
        if i.cat:
            cnt[i.cat] = cnt.get(i.cat, 0) + 1
    print(f"[info] tanınan hissələr: {cnt}", file=sys.stderr)

    res, err = optimize(items, a.budget)
    if err:
        print(err)
        sys.exit(1)
    build, warns = res
    if a.details:
        import requests
        session = session or requests.Session()
        for i in build.values():
            i.condition = fetch_condition(session, i.url)
            time.sleep(a.delay)
    print(render(build, warns, a.budget))


if __name__ == "__main__":
    main()
