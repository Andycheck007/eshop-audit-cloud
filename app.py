import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import json
import base64
from io import BytesIO
from PIL import Image
import google.generativeai as genai

# ── Konfigurácia stránky ──
st.set_page_config(page_title="E-shop Audit Tool", page_icon="🔍", layout="wide")

st.title("🔍 E-shop Audit Tool")
st.markdown(
    "Zadaj URL e-shopu, klikni **Spustiť audit** a dostaneš kompletný UX, SEO "
    "a konverzný report s konkrétnymi návrhmi na zlepšenie."
)

# ── Vstupy ──
gemini_key = st.text_input("🔑 Gemini API kľúč", type="password")
homepage_url = st.text_input("🏠 URL hlavnej stránky e-shopu", placeholder="https://www.example.sk/")

st.subheader("📄 Podstránky na audit (max 10)")
st.caption("Zadaj relatívne cesty (napr. /produkty/) alebo plné URL, každú na nový riadok")
subpages_text = st.text_area(
    "Podstránky",
    value="/kategoria/\n/produkt/\n/kosik/\n/kontakt/\n/blog/",
    height=150,
)


# ── Pomocné funkcie ──

def normalize_url(base, path):
    """Spojí base URL a relatívnu cestu."""
    path = path.strip()
    if not path:
        return None
    if path.startswith("http"):
        return path
    return urljoin(base, path)


def fetch_html(url, timeout=15):
    """Stiahne HTML stránky."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception as e:
        return None


def analyze_html(url, html):
    """Extrahuje SEO a štruktúrne dáta z HTML."""
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag.get("content", "") if meta_desc_tag else ""

    meta_viewport = soup.find("meta", attrs={"name": "viewport"})
    has_viewport = meta_viewport is not None

    canonical_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical = canonical_tag.get("href", "") if canonical_tag else ""

    og_tags = {}
    for og in soup.find_all("meta", attrs={"property": lambda x: x and x.startswith("og:")}):
        og_tags[og.get("property", "")] = og.get("content", "")

    headings = {}
    for level in range(1, 7):
        tag = f"h{level}"
        found = soup.find_all(tag)
        if found:
            headings[tag] = [h.get_text(strip=True)[:100] for h in found[:10]]

    images = soup.find_all("img")
    total_images = len(images)
    images_without_alt = []
    for img in images:
        alt = img.get("alt", None)
        if alt is None or alt.strip() == "":
            src = img.get("src", "")[:120]
            images_without_alt.append(src)

    links = soup.find_all("a", href=True)
    internal_links = 0
    external_links = 0
    parsed_base = urlparse(url)
    for link in links:
        href = link["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        parsed_href = urlparse(urljoin(url, href))
        if parsed_href.netloc == parsed_base.netloc:
            internal_links += 1
        else:
            external_links += 1

    scripts = soup.find_all("script", src=True)
    stylesheets = soup.find_all("link", attrs={"rel": "stylesheet"})

    forms = soup.find_all("form")
    has_search = any(
        inp.get("type") == "search"
        or "search" in (inp.get("name", "") + inp.get("placeholder", "")).lower()
        for form in forms
        for inp in form.find_all("input")
    )

    structured_data = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                structured_data.append(data.get("@type", "unknown"))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        structured_data.append(item.get("@type", "unknown"))
        except Exception:
            pass

    text_content = soup.get_text(separator=" ", strip=True)
    word_count = len(text_content.split())

    return {
        "url": url,
        "title": title,
        "title_length": len(title),
        "meta_description": meta_desc,
        "meta_description_length": len(meta_desc),
        "has_viewport": has_viewport,
        "canonical": canonical,
        "og_tags": og_tags,
        "headings": headings,
        "total_images": total_images,
        "images_without_alt": images_without_alt[:15],
        "images_without_alt_count": len(images_without_alt),
        "internal_links": internal_links,
        "external_links": external_links,
        "scripts_count": len(scripts),
        "stylesheets_count": len(stylesheets),
        "has_search": has_search,
        "forms_count": len(forms),
        "structured_data_types": structured_data,
        "word_count": word_count,
    }


def get_screenshot_url(url):
    """Vráti URL screenshotu cez bezplatnú API službu."""
    # Používame Google PageSpeed screenshot alebo thum.io
    return f"https://image.thum.io/get/width/1280/crop/800/noanimate/{url}"


def get_pagespeed_data(url, strategy="mobile"):
    """Získa dáta z PageSpeed Insights API (zadarmo, bez kľúča)."""
    api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "strategy": strategy,
        "category": ["performance", "accessibility", "best-practices", "seo"],
    }
    try:
        resp = requests.get(api_url, params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()

        categories = data.get("lighthouseResult", {}).get("categories", {})
        scores = {}
        for key, val in categories.items():
            scores[key] = round(val.get("score", 0) * 100)

        audits = data.get("lighthouseResult", {}).get("audits", {})
        metrics = {}
        for metric_key in [
            "first-contentful-paint",
            "largest-contentful-paint",
            "total-blocking-time",
            "cumulative-layout-shift",
            "speed-index",
            "interactive",
        ]:
            if metric_key in audits:
                metrics[metric_key] = {
                    "displayValue": audits[metric_key].get("displayValue", ""),
                    "score": audits[metric_key].get("score", 0),
                }

        # Screenshot z PageSpeed
        screenshot_data = None
        screenshot_audit = audits.get("final-screenshot", {})
        if screenshot_audit:
            details = screenshot_audit.get("details", {})
            screenshot_data = details.get("data", None)

        return {
            "strategy": strategy,
            "scores": scores,
            "metrics": metrics,
            "screenshot_base64": screenshot_data,
        }
    except Exception as e:
        return {"strategy": strategy, "error": str(e)}


def run_audit_for_url(url):
    """Kompletný audit jednej URL – HTML analýza + PageSpeed."""
    html = fetch_html(url)
    if html is None:
        return {"url": url, "error": "Nepodarilo sa stiahnuť stránku"}

    seo_data = analyze_html(url, html)

    ps_mobile = get_pagespeed_data(url, "mobile")
    ps_desktop = get_pagespeed_data(url, "desktop")

    return {
        "seo": seo_data,
        "pagespeed_mobile": ps_mobile,
        "pagespeed_desktop": ps_desktop,
    }


def generate_gemini_report(gemini_key, all_results, homepage_url):
    """Vygeneruje audit report cez Gemini."""
    genai.configure(api_key=gemini_key)

    # Odstráň screenshot base64 z promptu (príliš veľké)
    results_for_prompt = []
    for r in all_results:
        entry = dict(r)
        if "pagespeed_mobile" in entry and entry["pagespeed_mobile"].get("screenshot_base64"):
            entry["pagespeed_mobile"] = {
                k: v
                for k, v in entry["pagespeed_mobile"].items()
                if k != "screenshot_base64"
            }
        if "pagespeed_desktop" in entry and entry["pagespeed_desktop"].get("screenshot_base64"):
            entry["pagespeed_desktop"] = {
                k: v
                for k, v in entry["pagespeed_desktop"].items()
                if k != "screenshot_base64"
            }
        results_for_prompt.append(entry)

    prompt = f"""Si expert na UX, SEO a konverzné optimalizácie e-shopov.

Dostal si dáta z auditu e-shopu {homepage_url}. Na základe týchto dát vytvor KOMPLETNÝ PROFESIONÁLNY AUDIT REPORT v slovenčine.

DÁTA Z AUDITU:
{json.dumps(results_for_prompt, indent=2, ensure_ascii=False)}

ŠTRUKTÚRA REPORTU:

## 📊 Celkové zhrnutie
- Celkové hodnotenie e-shopu (1-10)
- Top 3 silné stránky
- Top 3 kritické problémy

## 🔍 SEO Audit
Pre každú stránku:
- Title tag – dĺžka, kvalita, odporúčanie
- Meta description – dĺžka, kvalita, odporúčanie
- Heading štruktúra – správnosť hierarchie
- Obrázky bez alt textu – počet a dopad
- Interné/externé linky
- Štruktúrované dáta
- Canonical URL

## ⚡ Výkon a rýchlosť
- Core Web Vitals (LCP, CLS, TBT)
- Performance score mobile vs desktop
- Konkrétne odporúčania na zrýchlenie

## 📱 UX a mobilná verzia
- Viewport nastavenie
- Mobile vs desktop skóre
- Accessibility skóre
- Odporúčania na zlepšenie UX

## 🛒 Konverzné odporúčania
- Analýza formulárov a vyhľadávania
- CTA prvky
- Nákupný proces
- Trust signály

## ✅ Akčný plán
Zoraď odporúčania podľa priority (vysoká/stredná/nízka) a odhadovaného dopadu.
Použi formát tabuľky: | Priorita | Odporúčanie | Dopad | Náročnosť |

Buď konkrétny, uvádzaj presné hodnoty z dát. Nepoužívaj všeobecné frázy."""

    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(prompt)
    return response.text


# ── Hlavná logika ──

if st.button("🔍 Spustiť audit", type="primary", use_container_width=True):
    if not gemini_key:
        st.error("Zadaj Gemini API kľúč.")
        st.stop()
    if not homepage_url:
        st.error("Zadaj URL e-shopu.")
        st.stop()

    # Zozbieraj URL
    urls = [homepage_url.strip().rstrip("/") + "/"]
    for line in subpages_text.strip().split("\n"):
        normalized = normalize_url(homepage_url, line)
        if normalized and normalized not in urls:
            urls.append(normalized)

    urls = urls[:11]  # max 11 stránok

    st.info(f"📄 Auditujem {len(urls)} stránok...")

    # Audit každej stránky
    all_results = []
    progress_bar = st.progress(0)

    for i, url in enumerate(urls):
        st.write(f"⏳ Analyzujem: `{url}`")
        result = run_audit_for_url(url)
        all_results.append(result)
        progress_bar.progress((i + 1) / len(urls))

    progress_bar.empty()

    # ── Screenshoty stránok ──
    st.subheader("📸 Screenshoty stránok")

    for result in all_results:
        if "error" in result:
            continue
        url = result["seo"]["url"]

        # Screenshot z PageSpeed API (ak existuje)
        for variant in ["pagespeed_mobile", "pagespeed_desktop"]:
            ps = result.get(variant, {})
            screenshot = ps.get("screenshot_base64")
            if screenshot and screenshot.startswith("data:image"):
                try:
                    img_data = screenshot.split(",")[1]
                    img_bytes = base64.b64decode(img_data)
                    img = Image.open(BytesIO(img_bytes))
                    label = "📱 Mobile" if "mobile" in variant else "🖥️ Desktop"
                    st.markdown(f"**{label}: {url}**")
                    st.image(img, use_container_width=True)
                except Exception:
                    pass

        # Záložný screenshot cez thum.io
        st.markdown(f"**🖼️ Náhľad: {url}**")
        st.image(get_screenshot_url(url), use_container_width=True)

    # ── PageSpeed skóre ──
    st.subheader("⚡ PageSpeed skóre")
    for result in all_results:
        if "error" in result:
            st.warning(f"❌ {result.get('url', 'N/A')}: {result['error']}")
            continue
        url = result["seo"]["url"]
        col1, col2 = st.columns(2)
        for col, variant, label in [
            (col1, "pagespeed_mobile", "📱 Mobile"),
            (col2, "pagespeed_desktop", "🖥️ Desktop"),
        ]:
            ps = result.get(variant, {})
            if "error" in ps:
                col.warning(f"{label}: Chyba – {ps['error']}")
            else:
                scores = ps.get("scores", {})
                col.markdown(f"**{label}: {url}**")
                score_cols = col.columns(4)
                for j, (key, name) in enumerate(
                    [
                        ("performance", "Výkon"),
                        ("accessibility", "Prístupnosť"),
                        ("best-practices", "Best Practices"),
                        ("seo", "SEO"),
                    ]
                ):
                    score = scores.get(key, "–")
                    if isinstance(score, int) and score >= 90:
                        color = "🟢"
                    elif isinstance(score, int) and score >= 50:
                        color = "🟡"
                    else:
                        color = "🔴"
                    score_cols[j].metric(name, f"{color} {score}")

    # ── AI Report ──
    st.subheader("📝 Generujem AI audit report...")
    with st.spinner("Gemini analyzuje dáta a píše report..."):
        try:
            report = generate_gemini_report(gemini_key, all_results, homepage_url)
            st.markdown("---")
            st.markdown(report)

            st.download_button(
                label="📥 Stiahnuť report ako TXT",
                data=report,
                file_name="eshop_audit_report.txt",
                mime="text/plain",
            )
        except Exception as e:
            st.error(f"Chyba pri generovaní reportu: {e}")
