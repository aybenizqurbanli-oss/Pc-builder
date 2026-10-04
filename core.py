"""Scraping + klassifikasiya + uyğunluq + optimizasiya (Streamlit-dən asılı deyil)."""
import itertools
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from db import (CHIPSET_SOCKET, GPU_DB, INTEL_DB, MB_TIER, RYZEN_DB,
                SOCKET_DEFAULT_DDR)

BASE = "https://tap.az"
DEFAULT_URL_TMPL = "https://tap.az/elanlar?keywords={q}&page={p}"
DEFAULT_QUERIES = [
    "ryzen", "intel core i3", "intel core i5", "intel core i7",
    "rtx", "gtx", "radeon rx", "ana plata", "ddr4", "ddr5",
    "ssd", "hdd", "qida bloku", "korpus",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "az,en;q=0.8",
}

CATS = ["cpu", "mb", "gpu", "ram", "ssd", "hdd", "psu", "case"]
LABELS = {"cpu": "Prosessor", "mb": "Ana plata", "gpu": "Videokart", "ram": "RAM",
          "ssd": "SSD", "hdd": "HDD", "psu": "Qida bloku", "case": "Korpus"}

PSU_HEADROOM = 1.3
CAP_FACTOR = 1.6
TOP_N = 6

PURPOSES = {
    "🎮 Oyun": dict(
        split=dict(gpu=.38, cpu=.20, mb=.11, ram=.08, ssd=.07, psu=.08, case=.05),
        cpu_key="game", w=dict(gpu=.55, cpu=.30, ram=1.0, ssd=.02),
        ram_cap=32, ssd_cap=1000, min_ram=16, min_ssd=240,
        gpu_optional=False, bottleneck=True, price_pen=0.0),
    "🎨 Render / Dizayn": dict(
        split=dict(gpu=.30, cpu=.28, mb=.11, ram=.13, ssd=.07, psu=.07, case=.04),
        cpu_key="mt", w=dict(gpu=.40, cpu=.35, ram=1.5, ssd=.02),
        ram_cap=64, ssd_cap=2000, min_ram=32, min_ssd=480,
        gpu_optional=False, bottleneck=False, price_pen=0.0),
    "💼 Ofis / Təhsil": dict(
        split=dict(gpu=.10, cpu=.30, mb=.15, ram=.13, ssd=.12, psu=.10, case=.08),
        cpu_key="mt", w=dict(gpu=.05, cpu=.50, ram=1.0, ssd=.03),
        ram_cap=16, ssd_cap=512, min_ram=8, min_ssd=240,
        gpu_optional=True, bottleneck=False, price_pen=0.15),
}


@dataclass
class Item:
    title: str
    price: float
    url: str
    image: Optional[str] = None
    condition: str = "?"
    cat: Optional[str] = None
    spec: dict = field(default_factory=dict)
    dummy: bool = False


# =============================================================================
# SCRAPING
# =============================================================================
PRICE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)\s*(?:AZN|₼)", re.I)
AD_HREF = re.compile(r"/elanlar/(?:[\w\-]+/)*\d{5,}")


def _img_url(box):
    img = box.find("img")
    if not img:
        return None
    for attr in ("data-src", "data-original", "data-lazy-src", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return urljoin(BASE, v)
    ss = img.get("srcset") or img.get("data-srcset")
    if ss:
        return urljoin(BASE, ss.split()[0])
    return None


def parse_listing(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=AD_HREF):
        url = urljoin(BASE, a["href"].split("?")[0])
        if url in out:
            continue
        box = a
        for _ in range(4):
            if PRICE_RE.search(box.get_text(" ", strip=True)) or box.parent is None:
                break
            box = box.parent
        pm = PRICE_RE.search(box.get_text(" ", strip=True))
        if not pm:
            continue
        price = float(re.sub(r"\D", "", pm.group(1)))
        name_el = box.select_one(".products-name, .product-name, [class*=name], "
                                 "[class*=title], h3, h2")
        title = (name_el.get_text(" ", strip=True) if name_el
                 else a.get("title") or a.get_text(" ", strip=True))
        title = PRICE_RE.sub("", title).strip()
        if not title:
            img = box.find("img")
            title = (img.get("alt") if img else "") or ""
        if not title:
            continue
        out[url] = Item(title=title, price=price, url=url, image=_img_url(box))
    return list(out.values())


def scrape(queries, pages, delay, tmpl=DEFAULT_URL_TMPL, progress=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    items, errors = {}, []
    n = max(len(queries), 1)
    for qi, q in enumerate(queries):
        for p in range(1, pages + 1):
            url = tmpl.format(q=quote_plus(q), p=p)
            try:
                r = s.get(url, timeout=20)
                if r.status_code != 200:
                    errors.append(f"{q} (səh.{p}): HTTP {r.status_code}")
                    break
                found = parse_listing(r.text)
            except Exception as e:  # noqa
                errors.append(f"{q}: {e}")
                break
            for it in found:
                items.setdefault(it.url, it)
            if progress:
                progress(min((qi + p / pages) / n, 1.0))
            if not found:
                break
            time.sleep(delay)
    return list(items.values()), errors


def fetch_details(url: str):
    """Elan səhifəsindən vəziyyət (Yeni/İşlənmiş) və şəkil."""
    cond, img = "?", None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        label = soup.find(string=re.compile(r"Vəziyyət", re.I))
        if label:
            cell = label.find_parent(["td", "dt", "span", "div", "li"])
            nxt = cell.find_next_sibling() if cell else None
            if nxt:
                cond = nxt.get_text(" ", strip=True)
        if cond == "?":
            txt = soup.get_text(" ", strip=True).lower()
            if "işlənmiş" in txt or "ikinci əl" in txt:
                cond = "İşlənmiş"
            elif "yeni" in txt:
                cond = "Yeni"
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img = og["content"]
    except Exception:  # noqa
        pass
    return {"condition": cond, "image": img}


# =============================================================================
# KLASSİFİKASİYA
# =============================================================================
def norm(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[\-_/,+()]+", " ", t)
    t = re.sub(r"(rtx|gtx|rx|gt)\s*(\d)", r"\1 \2", t)
    t = re.sub(r"(\d{3,4})\s*(ti|xt|xtx|super|gre)\b", r"\1 \2", t)
    return re.sub(r"\s+", " ", t).strip()


def _db_lookup(db, model):
    for key in sorted(db, key=len, reverse=True):
        if model.startswith(key):
            return db[key]
    return None


def detect_cpu(t):
    m = re.search(r"ryzen\s*[3579]\s*(?:pro\s*)?(\d{4}[a-z0-9]*)", t)
    if m:
        model = m.group(1)
        sock = "AM5" if model[0] in "789" else "AM4"
        v = _db_lookup(RYZEN_DB, model)
        if not v:
            return None
        igpu = ("g" in re.sub(r"^\d{4}", "", model)[:2] if sock == "AM4"
                else not model.endswith("f"))
        return dict(socket=sock, game=v[0], mt=v[1], tdp=v[2], model="Ryzen " + model, igpu=igpu)
    m = re.search(r"(?<![a-z])i[3579]\s*(\d{4,5}[a-z]*)", t)
    if m:
        model = m.group(1)
        digits = re.match(r"\d+", model).group()
        gen = int(digits[:2]) if len(digits) == 5 else int(digits[0])
        if gen >= 12:
            sock = "LGA1700"
        elif gen in (10, 11):
            sock = "LGA1200"
        elif gen in (8, 9):
            sock = "LGA1151v2"
        elif gen in (6, 7):
            sock = "LGA1151v1"
        else:
            return None
        v = _db_lookup(INTEL_DB, model)
        if not v:
            return None
        suffix = model[len(digits):]
        return dict(socket=sock, game=v[0], mt=v[1], tdp=v[2], model="Core " + model,
                    igpu="f" not in suffix)
    return None


def detect_gpu(t):
    for key in sorted(GPU_DB, key=len, reverse=True):
        if re.search(rf"(?<![\w]){re.escape(key)}(?![\d])", t):
            perf, tdp = GPU_DB[key]
            return dict(perf=perf, tdp=tdp, model=key.upper())
    return None


def detect_board(t):
    for chip in CHIPSET_SOCKET:
        if re.search(rf"(?<![a-z0-9]){chip}(?![0-9])", t):
            sock = CHIPSET_SOCKET[chip]
            d = re.search(r"(?<![gl])ddr\s*([45])", t)
            return dict(socket=sock, chip=chip.upper(), tier=MB_TIER.get(chip, 2),
                        ddr=f"DDR{d.group(1)}" if d else SOCKET_DEFAULT_DDR.get(sock))
    return None


def detect_ram(t):
    d = re.search(r"(?<![gl])ddr\s*([345])", t)
    if not d:
        return None
    k = re.search(r"(\d)\s*x\s*(\d{1,3})\s*gb", t)
    if k:
        gb = int(k.group(1)) * int(k.group(2))
    else:
        g = re.search(r"(\d{1,3})\s*gb", t)
        if not g:
            return None
        gb = int(g.group(1))
    return dict(ddr=f"DDR{d.group(1)}", gb=gb)


def _cap_gb(t):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)", t)
    if not m:
        return None
    return int(float(m.group(1)) * (1000 if m.group(2) == "tb" else 1))


def classify(it: Item):
    t = norm(it.title)
    if re.search(r"noutbuk|notebook|laptop|sodimm|so dimm|macbook|monitor|telefon|iphone", t):
        return
    hits = {}
    if (g := detect_gpu(t)) and re.search(r"rtx|gtx|rx|gt|radeon|geforce|arc|video", t):
        hits["gpu"] = g
    if c := detect_cpu(t):
        hits["cpu"] = c
    if b := detect_board(t):
        hits["mb"] = b
    if not hits:
        if re.search(r"\bssd\b|nvme|m 2", t) and (gb := _cap_gb(t)):
            hits["ssd"] = dict(gb=gb)
        elif re.search(r"\bhdd\b|hard disk|sərt disk|sabit disk", t) and (gb := _cap_gb(t)):
            hits["hdd"] = dict(gb=gb)
        elif (r := detect_ram(t)):
            hits["ram"] = r
        elif re.search(r"psu|qida blok|power supply|блок питания", t) and \
                (w := re.search(r"(\d{3,4})\s*(?:w|watt|vt)\b", t)):
            hits["psu"] = dict(watt=int(w.group(1)))
        elif re.search(r"korpus|\bcase\b|gövdə|midi tower|full tower|mini tower", t):
            hits["case"] = {}
    if len(hits) == 1:  # bundle / qeyri-müəyyən elanlar atlanır
        it.cat, it.spec = next(iter(hits.items()))


# =============================================================================
# UYĞUNLUQ
# =============================================================================
def required_psu(cpu, gpu):
    return int((cpu.spec["tdp"] + gpu.spec.get("tdp", 0) + 100) * PSU_HEADROOM)


def check_build(b):
    """[(status, mətn)] ; status: True=ok, False=xəta, None=xəbərdarlıq"""
    cpu, mb, ram, gpu, psu = b["cpu"], b["mb"], b["ram"], b["gpu"], b["psu"]
    res = []
    res.append((cpu.spec["socket"] == mb.spec["socket"],
                f"Soket: CPU {cpu.spec['socket']} ↔ Ana plata {mb.spec['socket']} ({mb.spec['chip']})"))
    if mb.spec.get("ddr"):
        res.append((mb.spec["ddr"] == ram.spec["ddr"],
                    f"RAM: {ram.spec['ddr']} ↔ ana plata {mb.spec['ddr']}"))
    else:
        res.append((None, f"RAM: ana platanın DDR növü başlıqdan bilinmir, RAM {ram.spec['ddr']} - "
                          "alarkən yoxla"))
    need = required_psu(cpu, gpu)
    res.append((psu.spec["watt"] >= need,
                f"PSU: {psu.spec['watt']}W ≥ tələb olunan ~{need}W "
                f"(CPU {cpu.spec['tdp']}W + GPU {gpu.spec.get('tdp', 0)}W + 100W) × {PSU_HEADROOM}"))
    if gpu.dummy:
        res.append((cpu.spec["igpu"], "Ayrıca videokart yoxdur - prosessorun daxili qrafikası istifadə olunur"))
    return res


# =============================================================================
# OPTİMİZASİYA
# =============================================================================
def _rank(it, cat, cfg):
    if cat == "gpu":
        return it.spec["perf"]
    if cat == "cpu":
        return it.spec[cfg["cpu_key"]]
    if cat in ("ram", "ssd", "hdd"):
        return it.spec["gb"]
    if cat == "psu":
        return it.spec["watt"]
    if cat == "mb":
        return it.spec["tier"] * 10
    return 1


def _shortlist(pool, cat, cfg, n=TOP_N):
    by_perf = sorted(pool, key=lambda i: -_rank(i, cat, cfg))[:n]
    by_val = sorted(pool, key=lambda i: -(_rank(i, cat, cfg) / i.price))[:n]
    seen, res = set(), []
    for i in by_perf + by_val:
        if i.url not in seen:
            seen.add(i.url)
            res.append(i)
    return res


def _score(cfg, gpu, cpu, mb, ram, ssd, total):
    w = cfg["w"]
    s = (w["gpu"] * gpu.spec["perf"] + w["cpu"] * cpu.spec[cfg["cpu_key"]]
         + w["ram"] * min(ram.spec["gb"], cfg["ram_cap"])
         + w["ssd"] * min(ssd.spec["gb"], cfg["ssd_cap"]) + 3 * mb.spec["tier"])
    if cfg["bottleneck"] and gpu.spec["perf"]:
        ratio = gpu.spec["perf"] / max(cpu.spec["game"], 1)
        s -= 60 * max(0.0, ratio - 3.2)  # çox güclü GPU + zəif CPU cəzası
    return s - cfg["price_pen"] * total


def optimize(items, budget, purpose, want_hdd=False):
    """-> (build_dict | None, error_str | None)"""
    cfg = PURPOSES[purpose]
    caps = {c: budget * cfg["split"][c] * CAP_FACTOR for c in cfg["split"]}
    by = {c: [i for i in items if i.cat == c and 0 < i.price <= caps.get(c, budget)] for c in CATS}

    def keep(lst, pred):
        f = [i for i in lst if pred(i)]
        return f or lst  # çatmırsa tələbi yumşalt

    gpus = _shortlist(by["gpu"], "gpu", cfg) if by["gpu"] else []
    if cfg["gpu_optional"]:
        gpus.append(Item("Daxili qrafika (iGPU)", 0, "", cat="gpu", spec=dict(perf=0, tdp=0), dummy=True))
    cpus = _shortlist(by["cpu"], "cpu", cfg) if by["cpu"] else []
    mbs = {}
    for sock in {i.spec["socket"] for i in by["mb"]}:
        mbs[sock] = _shortlist([i for i in by["mb"] if i.spec["socket"] == sock], "mb", cfg, 3)
    rams = _shortlist(keep(by["ram"], lambda i: i.spec["gb"] >= cfg["min_ram"]), "ram", cfg) if by["ram"] else []
    ssds = _shortlist(keep(by["ssd"], lambda i: i.spec["gb"] >= cfg["min_ssd"]), "ssd", cfg) if by["ssd"] else []
    psus = sorted(keep(by["psu"], lambda i: i.spec["watt"] >= 400), key=lambda i: i.price)
    cases = sorted(by["case"], key=lambda i: i.price)
    case = cases[0] if cases else None

    missing = [LABELS[c] for c, v in [("gpu", gpus), ("cpu", cpus), ("mb", mbs), ("ram", rams),
                                      ("ssd", ssds), ("psu", psus), ("case", cases)] if not v]
    if missing:
        return None, ("Bu büdcə üçün uyğun elan tapılmadı: " + ", ".join(missing)
                      + ". Büdcəni artır və ya daha çox səhifə/açar söz yüklə.")

    best, best_key = None, None
    for gpu, cpu in itertools.product(gpus, cpus):
        if gpu.dummy and not cpu.spec["igpu"]:
            continue
        p1 = gpu.price + cpu.price + case.price
        if p1 > budget:
            continue
        need = required_psu(cpu, gpu)
        psu = next((p for p in psus if p.spec["watt"] >= need), None)
        if not psu:
            continue
        p1 += psu.price
        for mb in mbs.get(cpu.spec["socket"], []):
            p2 = p1 + mb.price
            if p2 > budget:
                continue
            for ram in rams:
                if mb.spec["ddr"] and mb.spec["ddr"] != ram.spec["ddr"]:
                    continue
                p3 = p2 + ram.price
                if p3 > budget:
                    continue
                for ssd in ssds:
                    total = p3 + ssd.price
                    if total > budget:
                        continue
                    key = (_score(cfg, gpu, cpu, mb, ram, ssd, total), -total)
                    if best_key is None or key > best_key:
                        best_key = key
                        best = dict(cpu=cpu, mb=mb, gpu=gpu, ram=ram, ssd=ssd, psu=psu, case=case)
    if not best:
        return None, ("Uyğun (soket/DDR/PSU) və büdcəyə sığan kombinasiya tapılmadı. "
                      "Büdcəni artır və ya daha çox elan yüklə.")
    if want_hdd:
        left = budget - sum(i.price for i in best.values())
        hdds = [h for h in items if h.cat == "hdd" and 0 < h.price <= min(left, budget * 0.06)]
        if hdds:
            best["hdd"] = max(hdds, key=lambda h: (h.spec["gb"], -h.price))
    return best, None


def spec_text(it: Item) -> str:
    s, c = it.spec, it.cat
    if c == "cpu":
        return f"{s['model']} · soket {s['socket']} · {s['tdp']}W" + (" · iGPU" if s["igpu"] else "")
    if c == "gpu":
        return "Daxili qrafika" if it.dummy else f"{s['model']} · ~{s['tdp']}W"
    if c == "mb":
        return f"{s['chip']} · {s['socket']} · {s['ddr'] or 'DDR ?'}"
    if c == "ram":
        return f"{s['ddr']} · {s['gb']} GB"
    if c in ("ssd", "hdd"):
        return f"{s['gb']} GB"
    if c == "psu":
        return f"{s['watt']} W"
    return ""
