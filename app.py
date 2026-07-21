import streamlit as st
import asyncio
import os
import json
import re
import subprocess
import sys
from datetime import datetime
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from google import genai

# ============================================================
# PLAYWRIGHT SETUP (pre cloud)
# ============================================================

@st.cache_resource
def install_playwright():
    """Nainštaluje Chromium pri prvom spustení."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True
    )
    return True

install_playwright()

# ============================================================
# KONFIGURÁCIA
# ============================================================

st.set_page_config(
    page_title="E-shop Audit Tool",
    page_icon="🔍",
    layout="wide"
)

# ============================================================
# POMOCNÉ FUNKCIE
# ============================================================

def markdown_to_html(md_text):
    """Jednoduchý prevod Markdown na HTML."""
    lines = md_text.split("\n")
    html_lines = []
    in_list = False
    in_code = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                html_lines.append("</pre>")
                in_code = False
            else:
                html_lines.append("<pre>")
                in_code = True
            continue

        if in_code:
            html_lines.append(line)
            continue

        if line.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h2>{line[2:]}</h2>")
        elif line.strip().startswith("- ") or line.strip().startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = re.sub(r"^\s*[-*]\s*", "", line)
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(r"`(.+?)`", r"<code>\1</code>", content)
            html_lines.append(f"<li>{content}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
            if line.strip():
                html_lines.append(f"<p>{line}</p>")

    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


async def ziskaj_data_stranky(url, viewport_width, viewport_height):
    """Odfotí stránku a vytiahne HTML metadata."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": viewport_width, "height": viewport_height}
        )

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            screenshot = await page.screenshot(full_page=True)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            title = soup.title.string if soup.title else "CHÝBA TITLE"

            meta_desc = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})
            if meta_tag:
                meta_desc = meta_tag.get("content", "")

            headings = {}
            for level in range(1, 7):
                tags = soup.find_all(f"h{level}")
                if tags:
                    headings[f"h{level}"] = [
                        tag.get_text(strip=True) for tag in tags
                    ]

            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if href.startswith("/") or url.split("//")[1].split("/")[0] in href:
                    links.append({"href": href, "text": text or "(bez textu)"})

            images_no_alt = []
            for img in soup.find_all("img"):
                alt = img.get("alt", "").strip()
                src = img.get("src", "")
                if not alt:
                    images_no_alt.append(src[:100])

            canonical = ""
            can_tag = soup.find("link", attrs={"rel": "canonical"})
            if can_tag:
                canonical = can_tag.get("href", "")

            seo_data = {
                "url": url,
                "title": title,
                "meta_description": meta_desc,
                "canonical": canonical,
                "headings": headings,
                "internal_links_count": len(links),
                "internal_links_sample": links[:20],
                "images_without_alt": images_no_alt[:10],
                "images_without_alt_count": len(images_no_alt),
            }

            await browser.close()
            return screenshot, seo_data

        except Exception as e:
            await browser.close()
            return None, {"url": url, "error": str(e)}


def analyzuj_gemini(api_key, screenshots_desktop, screenshots_mobile,
                    seo_data_list, hlavna_url, podstranky):
    """Pošle všetko do Gemini na analýzu."""

    client = genai.Client(api_key=api_key)

    seo_json = json.dumps(seo_data_list, ensure_ascii=False, indent=2)

    prompt = f"""
Si expert na UX/UI, SEO, konverzie a e-commerce s 15+ rokmi skúseností.
Proveď kompletný audit e-shopu na základe priložených screenshotov
a SEO dát.

HLAVNÁ STRÁNKA: {hlavna_url}
PODSTRÁNKY: {json.dumps(podstranky, ensure_ascii=False)}

SEO DÁTA (extrahované z HTML):
{seo_json}

ANALYZUJ TIETO OBLASTI:

### 1. ŠTRUKTÚRA WEBU A LOGIKA PODSTRÁNOK
- Je rozdelenie podstránok logické?
- Chýba niečo dôležité? (napr. referencie, galéria, cenník)
- Je hĺbka navigácie optimálna?

### 2. NÁZVOSLOVIE A URL SLUGY
- Sú URL slugy konzistentné a SEO-friendly?
- Sú názvy kategórií a stránok zrozumiteľné?

### 3. SEO ANALÝZA
- Title tagy: dĺžka, kľúčové slová, unikátnosť
- Meta description: kvalita, CTA, dĺžka
- Heading štruktúra (H1-H6): hierarchia, duplicity
- Alt texty obrázkov
- Interné linkovanie

### 4. LINKOVANIE
- Interné prepojenie medzi stránkami
- Breadcrumbs
- CTA linky smerujúce k konverzii

### 5. OBSAH A PRODUKTOVÉ POPISKY
- Kvalita a dĺžka textov
- Unikátnosť
- Odpovedajú texty na otázky zákazníkov?

### 6. UX/UI DESIGN
- Vizuálna hierarchia
- Konzistencia dizajnu
- Mobilná verzia (hodnoť s váhou 60%)

### 7. KONVERZIE
- CTA tlačidlá
- Nákupný proces
- Dôveryhodnosť

PRE KAŽDÚ OBLASŤ UVEĎ:
- ✅ Čo funguje dobre (konkrétne príklady)
- ❌ Čo je problém (konkrétne príklady)
- 💡 Doporučenie na zlepšenie (konkrétne kroky)
- 📊 Skóre 1-10

NAVYŠE PRE KAŽDÝ NÁJDENÝ PROBLÉM DODAJ HOTOVÉ RIEŠENIE:

SEO OPRAVY (copy-paste ready):
- Navrhni lepší TITLE tag (do 60 znakov)
- Navrhni lepší META DESCRIPTION (do 155 znakov, s CTA)
- Navrhni správnu H1-H6 štruktúru
- Navrhni ALT texty pre obrázky bez nich

TEXTOVÉ NÁVRHY:
- Prepíš slabé produktové popisky
- Navrhni lepšie CTA texty na tlačidlá
- Navrhni FAQ otázky, ktoré na webe chýbajú

UX ODPORÚČANIA:
- Konkrétne popisy čo kam presunúť
- Aké prvky pridať nad fold
- Ako zlepšiť mobilnú navigáciu

TECHNICKÉ ÚLOHY:
- Zoznam úloh pre vývojára (formát checklistu)
- Priorita: VYSOKÁ / STREDNÁ / NÍZKA
- Odhadovaný čas realizácie

NA KONCI VYTVOR:
1. CELKOVÉ SKÓRE (priemer všetkých oblastí)
2. TOP 5 PRIORÍT (čo opraviť ako prvé)
3. QUICK WINS (čo sa dá opraviť za menej ako 1 hodinu)
4. ODHAD DOPADU NA KONVERZIE (v %)

Odpovedaj v slovenčine. Buď konkrétny, uvádzaj príklady priamo
z analyzovaných stránok. Formátuj výstup tak, aby sa dal priamo
poslať vývojárovi alebo copywriterovi ako zadanie.
"""

    contents = [prompt]

    for url, screenshot in screenshots_desktop:
        if screenshot:
            contents.append(f"\n--- DESKTOP screenshot: {url} ---")
            contents.append(
                genai.types.Part.from_bytes(
                    data=screenshot,
                    mime_type="image/png"
                )
            )

    for url, screenshot in screenshots_mobile:
        if screenshot:
            contents.append(f"\n--- MOBILNÝ screenshot: {url} ---")
            contents.append(
                genai.types.Part.from_bytes(
                    data=screenshot,
                    mime_type="image/png"
                )
            )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )

    return response.text


# ============================================================
# STREAMLIT UI
# ============================================================

st.title("🔍 E-shop Audit Tool")
st.markdown(
    "Zadaj URL e-shopu, klikni **Spustiť audit** a dostaneš "
    "kompletný UX, SEO a konverzný report s konkrétnymi návrhmi na zlepšenie."
)

st.divider()

# API kľúč
api_key_env = os.environ.get("GEMINI_API_KEY", "")

if api_key_env:
    api_key = api_key_env
    st.success("🔑 API kľúč načítaný z nastavení")
else:
    api_key = st.text_input(
        "🔑 Gemini API kľúč",
        type="password",
        help="Získaš ho zadarmo na aistudio.google.com → Get API Key"
    )

hlavna_url = st.text_input(
    "🏠 URL hlavnej stránky e-shopu",
    placeholder="https://www.example.sk/"
)

st.markdown("#### 📄 Podstránky na audit (max 10)")
st.caption(
    "Zadaj relatívne cesty (napr. /produkty/) alebo plné URL, "
    "každú na nový riadok"
)

podstranky_text = st.text_area(
    "Podstránky",
    placeholder="/kategoria/\n/produkt/\n/kosik/\n/kontakt/\n/blog/",
    height=200
)

st.divider()

if st.button("🚀 Spustiť audit", type="primary", use_container_width=True):

    if not api_key:
        st.error("❌ Zadaj Gemini API kľúč!")
        st.stop()

    if not hlavna_url:
        st.error("❌ Zadaj URL hlavnej stránky!")
        st.stop()

    if not hlavna_url.startswith("http"):
        hlavna_url = "https://" + hlavna_url

    podstranky = [
        line.strip() for line in podstranky_text.strip().split("\n")
        if line.strip()
    ][:10]

    base = hlavna_url.rstrip("/")
    vsetky_url = [hlavna_url]
    for sub in podstranky:
        if sub.startswith("http"):
            vsetky_url.append(sub)
        else:
            vsetky_url.append(base + sub)

    st.info(f"📋 Auditujem {len(vsetky_url)} stránok (desktop + mobil)...")

    progress = st.progress(0)
    status = st.empty()

    screenshots_desktop = []
    screenshots_mobile = []
    seo_data_list = []

    total_steps = len(vsetky_url) * 2

    for i, url in enumerate(vsetky_url):
        status.text(f"💻 Desktop: {url}")
        screenshot, seo_data = asyncio.run(
            ziskaj_data_stranky(url, 1440, 900)
        )
        screenshots_desktop.append((url, screenshot))
        seo_data_list.append(seo_data)
        progress.progress((i * 2 + 1) / total_steps)

        status.text(f"📱 Mobil: {url}")
        screenshot_m, _ = asyncio.run(
            ziskaj_data_stranky(url, 390, 844)
        )
        screenshots_mobile.append((url, screenshot_m))
        progress.progress((i * 2 + 2) / total_steps)

    progress.progress(1.0)
    status.text("🤖 Analyzujem pomocou Gemini AI... (môže trvať 1-2 minúty)")

    with st.expander("📸 Screenshoty", expanded=False):
        for url, screenshot in screenshots_desktop:
            if screenshot:
                st.markdown(f"**💻 Desktop:** `{url}`")
                st.image(screenshot, use_container_width=True)
        for url, screenshot in screenshots_mobile:
            if screenshot:
                st.markdown(f"**📱 Mobil:** `{url}`")
                st.image(screenshot, width=390)

    with st.expander("🔍 Extrahované SEO dáta", expanded=False):
        for data in seo_data_list:
            st.json(data)

    try:
        vysledok = analyzuj_gemini(
            api_key, screenshots_desktop, screenshots_mobile,
            seo_data_list, hlavna_url, podstranky
        )

        st.divider()
        st.markdown("## 📊 Výsledky auditu")
        st.markdown(vysledok)

        # EXPORT
        st.divider()
        st.markdown("#### 📥 Stiahnuť report")

        datum = datetime.now().strftime("%Y-%m-%d")
        nazov_eshopu = (
            hlavna_url.split("//")[1].split("/")[0].replace("www.", "")
        )

        col1, col2 = st.columns(2)

        with col1:
            st.download_button(
                "📄 Markdown (.md)",
                data=vysledok,
                file_name=f"audit_{nazov_eshopu}_{datum}.md",
                mime="text/markdown",
                use_container_width=True
            )

        html_report = f"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Audit {nazov_eshopu} - {datum}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    line-height: 1.7; color: #1a1a1a;
    background: #f8f9fa; padding: 0;
}}
.header {{
    background: linear-gradient(135deg, #1e3a5f, #2d5a87);
    color: white; padding: 40px; text-align: center;
}}
.header h1 {{ font-size: 26px; margin-bottom: 8px; }}
.header p {{ opacity: 0.85; font-size: 15px; }}
.container {{ max-width: 900px; margin: 30px auto; padding: 0 20px; }}
.content {{
    background: white; border-radius: 12px;
    padding: 40px; box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}}
h2 {{
    color: #1e3a5f; border-bottom: 2px solid #e8ecf1;
    padding-bottom: 8px; margin: 32px 0 16px; font-size: 20px;
}}
h3 {{ color: #2d5a87; margin: 24px 0 12px; font-size: 17px; }}
code {{
    background: #f0f4f8; padding: 2px 8px;
    border-radius: 4px; font-size: 14px;
}}
pre {{
    background: #1e1e1e; color: #d4d4d4;
    padding: 16px; border-radius: 8px;
    overflow-x: auto; font-size: 13px;
}}
ul, ol {{ padding-left: 24px; margin: 8px 0; }}
li {{ margin: 4px 0; }}
.footer {{
    text-align: center; padding: 24px;
    color: #888; font-size: 13px;
}}
</style>
</head>
<body>
<div class="header">
    <h1>🔍 E-shop Audit Report</h1>
    <p>{nazov_eshopu} &bull; {datum}</p>
</div>
<div class="container">
    <div class="content">{markdown_to_html(vysledok)}</div>
</div>
<div class="footer">Vygenerované pomocou E-shop Audit Tool</div>
</body>
</html>"""

        with col2:
            st.download_button(
                "🌐 HTML report",
                data=html_report,
                file_name=f"audit_{nazov_eshopu}_{datum}.html",
                mime="text/html",
                use_container_width=True
            )

        status.text("✅ Audit dokončený!")

    except Exception as e:
        st.error(f"❌ Chyba pri analýze: {e}")

st.divider()
st.caption("🛠️ E-shop Audit Tool | Playwright + Gemini AI + Streamlit")
