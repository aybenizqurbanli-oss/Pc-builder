import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="Tap.az PC Builder", page_icon="🖥️", layout="wide")


# ---------------------------------------------------------------- cache-lər
@st.cache_data(ttl=1800, show_spinner="Tap.az-dan elanlar yüklənir (30-60 saniyə çəkə bilər)...")
def load_items(queries: tuple, pages: int, delay: float, tmpl: str):
    items, errors = core.scrape(list(queries), pages, delay, tmpl)
    for it in items:
        core.classify(it)
    return items, errors


@st.cache_data(ttl=3600, show_spinner=False)
def load_details(url: str):
    return core.fetch_details(url)


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Parametrlər")
    pages = st.slider("Hər axtarış üçün səhifə sayı", 1, 5, 2)
    delay = st.slider("Sorğular arası gecikmə (san)", 0.5, 3.0, 1.0, 0.5)
    tmpl = st.text_input("Axtarış URL şablonu", core.DEFAULT_URL_TMPL,
                         help="{q} = axtarış sözü, {p} = səhifə nömrəsi")
    queries = st.text_area("Axtarış sözləri (hər sətirdə biri)",
                           "\n".join(core.DEFAULT_QUERIES), height=220)
    if st.button("🔄 Məlumatı yenilə"):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------------- başlıq
st.title("🖥️ Tap.az PC Builder")
st.caption("Büdcəni və məqsədi seç - Tap.az elanlarından uyğun (soket / DDR / PSU) PC yığılsın.")

# büdcə: slider + number sinxron
st.session_state.setdefault("budget", 1500)


def _from_slider():
    st.session_state.budget = st.session_state.b_slider


def _from_number():
    st.session_state.budget = st.session_state.b_number


c1, c2 = st.columns([3, 1])
c1.slider("Büdcə (AZN)", 300, 6000, st.session_state.budget, 50,
          key="b_slider", on_change=_from_slider)
c2.number_input("və ya dəqiq məbləğ", 300, 20000, st.session_state.budget, 50,
                key="b_number", on_change=_from_number)
budget = st.session_state.budget

c3, c4 = st.columns([3, 1])
purpose = c3.radio("Məqsəd", list(core.PURPOSES), horizontal=True)
want_hdd = c4.checkbox("HDD də əlavə et (qalıq olsa)")

go = st.button("🚀 Setup Yığ", type="primary", use_container_width=True)

# ---------------------------------------------------------------- iş
if go:
    qs = tuple(q.strip() for q in queries.splitlines() if q.strip())
    items, errors = load_items(qs, pages, delay, tmpl)

    counts = {}
    for i in items:
        if i.cat:
            counts[i.cat] = counts.get(i.cat, 0) + 1
    with st.expander(f"📊 Yüklənən elanlar: {len(items)} (tanınan: {sum(counts.values())})"):
        st.write({core.LABELS[k]: v for k, v in counts.items()})
        unk = [i.title for i in items if not i.cat][:15]
        if unk:
            st.caption("Tanınmayan elanlardan nümunələr:")
            st.write(unk)
        for e in errors:
            st.warning(e)

    if not items:
        st.error("Tap.az-dan elan oxunmadı. Sayt sorğunu blok etmiş ola bilər, URL şablonu dəyişmiş ola "
                 "bilər və ya səhifə JavaScript ilə yüklənir. Tətbiqi lokal kompüterdə işlət və "
                 "sidebar-dakı URL şablonunu yoxla.")
        st.stop()

    build, err = core.optimize(items, budget, purpose, want_hdd)
    if err:
        st.error(err)
        st.stop()

    with st.spinner("Elan səhifələri yoxlanılır (vəziyyət və şəkil)..."):
        for it in build.values():
            if not it.dummy:
                d = load_details(it.url)
                it.condition = d["condition"]
                it.image = it.image or d["image"]

    total = sum(i.price for i in build.values())
    m1, m2, m3 = st.columns(3)
    m1.metric("💰 Yekun məbləğ", f"{total:,.0f} AZN")
    m2.metric("Büdcə", f"{budget:,.0f} AZN")
    m3.metric("Qalıq", f"{budget - total:,.0f} AZN")

    st.subheader("Yığılmış komponentlər")
    for cat in core.CATS:
        it = build.get(cat)
        if not it:
            continue
        with st.container(border=True):
            a, b, c = st.columns([1, 4, 2])
            if it.image:
                a.image(it.image, width=110)
            else:
                a.markdown("### 🖼️")
            b.markdown(f"**{core.LABELS[cat]}**  \n{it.title}")
            sp = core.spec_text(it)
            if sp:
                b.caption(sp + (f" · Vəziyyət: {it.condition}" if not it.dummy else ""))
            c.markdown(f"### {it.price:,.0f} AZN")
            if it.url:
                c.link_button("Tap.az-da aç", it.url)

    st.subheader("✅ Uyğunluq yoxlanışı")
    for ok, text in core.check_build(build):
        (st.success if ok else st.warning if ok is None else st.error)(text)

    st.subheader("📋 Cədvəl")
    rows = [{"Hissə": core.LABELS[c], "Ad": i.title, "Vəziyyət": i.condition,
             "Qiymət (AZN)": i.price, "Link": i.url or None}
            for c, i in build.items()]
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True,
                 column_config={"Link": st.column_config.LinkColumn("Tap.az linki", display_text="Aç"),
                                "Qiymət (AZN)": st.column_config.NumberColumn(format="%d")})
    st.markdown(f"**Yekun: {total:,.0f} AZN**")
    st.download_button("⬇️ CSV yüklə", df.to_csv(index=False).encode("utf-8-sig"),
                       "pc_setup.csv", "text/csv")
    st.caption("Qiymətlər və uyğunluq elan başlıqlarından avtomatik çıxarılır - alışdan əvvəl satıcı ilə "
               "dəqiqləşdir. Monitor, soyutma, klaviatura büdcəyə daxil deyil.")
