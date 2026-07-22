import streamlit as st
import requests
import os
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import json
import base64
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from PIL import Image
import google.generativeai as genai
import markdown2
from xhtml2pdf import pisa
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import tempfile
from fonts_data import DEJAVU_SANS_REGULAR_B64, DEJAVU_SANS_BOLD_B64

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# JS knižnica na kontrolu prístupnosti (WCAG), nahráva sa priamo do stránky v prehliadači
AXE_CORE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

# Streamlit Cloud beží vo vnútri asyncio event loopu (kvôli websocketom), a Playwright Sync API
# v takom vlákne zlyhá s chybou "Sync API inside the asyncio loop". Riešenie: spúšťať Playwright
# vždy v samostatnom vlákne bez bežiaceho event loopu – presne na to slúži tento worker.
_playwright_executor = ThreadPoolExecutor(max_workers=1)

# ── Konfigurácia stránky ──
st.set_page_config(page_title="E-shop Audit Tool", page_icon="🔍", layout="wide")

st.title("🔍 E-shop Audit Tool")
st.markdown(
    "Zadaj URL e-shopu, klikni **Spustiť audit** a dostaneš kompletný UX, SEO "
    "a konverzný report s konkrétnymi návrhmi na zlepšenie."
)

# ── Vstupy ──
gemini_key = st.text_input("🔑 Gemini API kľúč", type="password")
gemini_model_name = st.text_input(
    "🤖 Gemini model",
    value="gemini-3.5-flash",
    help="Google vypína konkrétne verzie modelov prekvapivo často. Ak dostaneš chybu "
         "'404 model not found', skús alias 'gemini-flash-latest' (ten sa aktualizuje sám), "
         "alebo si over aktuálny zoznam na https://ai.google.dev/gemini-api/docs/models.",
)
homepage_url = st.text_input("🏠 URL hlavnej stránky e-shopu", placeholder="https://www.example.sk/")

pagespeed_key = st.text_input(
    "🔑 Google PageSpeed API kľúč (voliteľné – zlepší spoľahlivosť)",
    type="password",
    help="Získaš ho zadarmo na https://console.cloud.google.com/apis/credentials. "
         "Bez kľúča má Google PageSpeed API veľmi nízky limit (cca 1 request/min) a ľahko dostaneš 429 chybu.",
)

col_a, col_b = st.columns(2)
with col_a:
    run_desktop_too = st.checkbox(
        "Analyzovať aj Desktop verziu (pomalšie, 2× viac requestov na PageSpeed API)",
        value=False,
    )
with col_b:
    use_playwright_render = st.checkbox(
        "Renderovať stránku ako skutočný prehliadač (odporúčané pre moderné e-shopy s JS obsahom)",
        value=PLAYWRIGHT_AVAILABLE,
        disabled=not PLAYWRIGHT_AVAILABLE,
        help="Vyžaduje Playwright s nainštalovaným Chromium. Bez toho sa použije jednoduchý HTTP request, "
             "ktorý nevidí obsah dorenderovaný JavaScriptom." if PLAYWRIGHT_AVAILABLE else
             "Playwright nie je v tomto prostredí dostupný – použije sa jednoduchý HTTP request.",
    )

st.subheader("📄 Podstránky na audit")
st.caption(
    "Poznámka: Nie je nutné (ani rozumné) auditovať celý e-shop stránku po stránke – "
    "e-shopy väčšinou používajú jednu šablónu pre všetky produkty/kategórie, takže chyba v šablóne "
    "sa opakuje na stovkách stránok rovnako. Stačí vybrať zástupcu za každý typ stránky "
    "(hlavná, kategória, produkt, košík, kontakt, blog)."
)

col_sm1, col_sm2 = st.columns([1, 2])
with col_sm1:
    find_sitemap_clicked = st.button("🔎 Nájsť URL zo sitemap.xml")
with col_sm2:
    max_pages = st.slider(
        "Max. počet podstránok na audit",
        min_value=1, max_value=20, value=5,
        help="Vyššie číslo = presnejší obraz o celom e-shope, ale dlhší beh a viac šancí na 429 "
             "chybu z PageSpeed. Odporúčané: 5-8 (jedna stránka za každý typ šablóny).",
    )

if find_sitemap_clicked:
    if not homepage_url:
        st.warning("Najprv vyplň URL hlavnej stránky e-shopu vyššie.")
    else:
        with st.spinner("Hľadám sitemap.xml..."):
            found_urls = discover_sitemap_urls(homepage_url)
        if found_urls:
            st.session_state["sitemap_urls"] = found_urls
            st.success(f"Našiel som {len(found_urls)} URL v sitemape. Vyber si z nich nižšie.")
        else:
            st.warning("Sitemap.xml sa nenašiel alebo je prázdny – vlož podstránky ručne do poľa nižšie.")

default_subpages = "/kategoria/\n/produkt/\n/kosik/\n/kontakt/\n/blog/"
if "sitemap_urls" in st.session_state:
    picked = st.multiselect(
        "Vyber podstránky nájdené v sitemap.xml (odporúčané: 1-2 z každého typu – kategória, produkt, statická stránka)",
        options=st.session_state["sitemap_urls"],
        default=st.session_state["sitemap_urls"][:max_pages],
    )
    default_subpages = "\n".join(picked)

subpages_text = st.text_area(
    "Podstránky (alebo uprav výber zo sitemap vyššie)",
    value=default_subpages,
    height=150,
)


# ── Pomocné funkcie ──

_dejavu_registered = False


def _register_unicode_font():
    """
    Zaregistruje DejaVu Sans (podporuje slovenskú diakritiku – č, š, ľ, ť, ž...).
    Font je zabudovaný priamo v kóde (fonts_data.py, base64), takže nezávisí od
    toho, či sa samostatný .ttf súbor podarilo správne nahrať na GitHub – binárne
    súbory sa cez webové rozhranie GitHubu občas nenahrajú spoľahlivo.
    Bez tohto fontu by xhtml2pdf použil predvolený Helvetica, ktorý diakritiku
    vykreslí ako čierne štvorčeky.
    """
    global _dejavu_registered
    if _dejavu_registered:
        return

    regular_bytes = base64.b64decode(DEJAVU_SANS_REGULAR_B64)
    bold_bytes = base64.b64decode(DEJAVU_SANS_BOLD_B64)

    regular_tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
    regular_tmp.write(regular_bytes)
    regular_tmp.close()

    bold_tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
    bold_tmp.write(bold_bytes)
    bold_tmp.close()

    pdfmetrics.registerFont(TTFont("DejaVuSans", regular_tmp.name))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold_tmp.name))
    pdfmetrics.registerFontFamily(
        "DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold",
        italic="DejaVuSans", boldItalic="DejaVuSans-Bold",
    )
    _dejavu_registered = True


def convert_report_to_pdf(markdown_text, homepage_url):
    """Skonvertuje vygenerovaný markdown report na PDF (bytes) na stiahnutie."""
    _register_unicode_font()
    html_body = markdown2.markdown(markdown_text, extras=["tables", "fenced-code-blocks"])
    html_full = f"""
    <html>
    <head>
    <meta charset="UTF-8">
    <style>
        @page {{ size: A4; margin: 2cm; }}
        body {{ font-family: "DejaVuSans"; font-size: 10pt; line-height: 1.5; color: #222; }}
        h1 {{ font-family: "DejaVuSans-Bold"; font-size: 18pt; color: #111; border-bottom: 2px solid #444; padding-bottom: 6px; }}
        h2 {{ font-family: "DejaVuSans-Bold"; font-size: 14pt; color: #222; margin-top: 20px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
        h3 {{ font-family: "DejaVuSans-Bold"; font-size: 12pt; color: #333; margin-top: 14px; }}
        b, strong {{ font-family: "DejaVuSans-Bold"; }}
        table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
        th, td {{ border: 1px solid #999; padding: 6px 8px; font-size: 9pt; text-align: left; font-family: "DejaVuSans"; }}
        th {{ background-color: #eee; font-family: "DejaVuSans-Bold"; }}
        code {{ background-color: #f2f2f2; padding: 1px 4px; }}
    </style>
    </head>
    <body>
        <h1>Audit e-shopu: {homepage_url}</h1>
        {html_body}
    </body>
    </html>
    """
    pdf_buffer = BytesIO()
    pisa.CreatePDF(html_full, dest=pdf_buffer, encoding="UTF-8")
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()


def discover_sitemap_urls(homepage_url, max_urls=200):
    """
    Skúsi nájsť sitemap.xml a vytiahnuť z neho zoznam URL adries e-shopu.
    Vráti zoznam URL (bez homepage), alebo prázdny zoznam ak sitemap neexistuje/zlyhá.
    """
    candidates = [
        urljoin(homepage_url, "/sitemap.xml"),
        urljoin(homepage_url, "/sitemap_index.xml"),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AuditBot/1.0)"}
    found = []
    for sitemap_url in candidates:
        try:
            resp = requests.get(sitemap_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "xml")
            locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
            # Ak je to sitemap index (odkazuje na ďalšie sitemapy), skús prvú z nich
            if locs and any(l.endswith(".xml") for l in locs):
                sub_sitemaps = [l for l in locs if l.endswith(".xml")][:3]
                for sub in sub_sitemaps:
                    try:
                        sub_resp = requests.get(sub, headers=headers, timeout=10)
                        sub_soup = BeautifulSoup(sub_resp.text, "xml")
                        found.extend([loc.get_text(strip=True) for loc in sub_soup.find_all("loc")])
                    except Exception:
                        continue
            else:
                found.extend(locs)
            if found:
                break
        except Exception:
            continue
    return found[:max_urls]


def normalize_url(base, path):
    """Spojí base URL a relatívnu cestu."""
    path = path.strip()
    if not path:
        return None
    if path.startswith("http"):
        return path
    return urljoin(base, path)


def fetch_html(url, timeout=15):
    """Stiahne HTML stránky cez obyčajný HTTP request (nevidí JavaScript obsah)."""
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
        return resp.text, dict(resp.headers)
    except Exception:
        return None, {}


_chromium_install_checked = False


def _ensure_chromium_installed():
    """Skontroluje/nainštaluje Chromium binárku pre Playwright. Volá sa len raz za beh appky."""
    global _chromium_install_checked
    if _chromium_install_checked:
        return
    _chromium_install_checked = True
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        else:
            raise


def _render_page_sync(url, timeout_ms=20000):
    """
    Skutočná Playwright logika – MUSÍ bežať v samostatnom vlákne bez asyncio event loopu.
    Volané výhradne cez fetch_rendered_page(), nikdy priamo z hlavného vlákna Streamlitu.
    """
    _ensure_chromium_installed()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # SPA s nekonečným pollingom – v poriadku, ideme ďalej

            html = page.content()
            headers = dict(response.headers) if response else {}

            screenshot_bytes = None
            try:
                screenshot_bytes = page.screenshot(full_page=False)
            except Exception:
                pass

            accessibility_issues = []
            try:
                page.add_script_tag(url=AXE_CORE_CDN)
                axe_result = page.evaluate("async () => { return await axe.run(); }")
                for violation in axe_result.get("violations", []):
                    accessibility_issues.append({
                        "id": violation.get("id"),
                        "impact": violation.get("impact"),
                        "description": violation.get("description"),
                        "help": violation.get("help"),
                        "nodes_affected": len(violation.get("nodes", [])),
                    })
            except Exception:
                pass  # axe-core zlyhal (napr. CSP blokuje externý script) – audit pokračuje bez neho

            context.close()

            return {
                "html": html,
                "headers": headers,
                "screenshot_bytes": screenshot_bytes,
                "accessibility_issues": accessibility_issues,
            }
        finally:
            browser.close()


def fetch_rendered_page(url, timeout_ms=20000):
    """
    Načíta stránku cez skutočný prehliadač (Playwright) – vidí obsah dorenderovaný JavaScriptom.
    Spúšťa sa v samostatnom vlákne, aby nekolidovalo s asyncio event loopom, v ktorom beží
    Streamlit Cloud (Playwright Sync API inak v takom vlákne zlyhá).
    Vráti: html, screenshot (bytes), response headers, zoznam accessibility chýb (axe-core),
    alebo {"error": "..."} ak zlyhá – volajúci má vtedy spraviť fallback na fetch_html.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright balíček nie je v tomto prostredí nainštalovaný."}

    try:
        future = _playwright_executor.submit(_render_page_sync, url, timeout_ms)
        return future.result(timeout=(timeout_ms / 1000) + 20)
    except Exception as e:
        return {"error": str(e)}


def check_security_headers(headers):
    """Vyhodnotí bezpečnostné HTTP hlavičky – lacná kontrola bez externého API."""
    headers_lower = {k.lower(): v for k, v in (headers or {}).items()}
    checks = {
        "https": None,  # dopĺňa sa mimo tejto funkcie podľa URL schémy
        "strict_transport_security": "strict-transport-security" in headers_lower,
        "content_security_policy": "content-security-policy" in headers_lower,
        "x_content_type_options": "x-content-type-options" in headers_lower,
        "x_frame_options": "x-frame-options" in headers_lower,
        "referrer_policy": "referrer-policy" in headers_lower,
    }
    return checks


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
    return f"https://image.thum.io/get/width/1280/crop/800/noanimate/{url}"


def get_pagespeed_data(url, strategy="mobile", api_key=None, max_retries=3):
    """
    Získa dáta z PageSpeed Insights API.
    Rieši 429 (Too Many Requests) exponenciálnym čakaním a retry.
    """
    api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "strategy": strategy,
        "category": ["performance", "accessibility", "best-practices", "seo"],
    }
    if api_key:
        params["key"] = api_key.strip()

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(api_url, params=params, timeout=90)

            if resp.status_code == 429:
                # Rate limit - počkaj a skús znova (exponenciálny backoff)
                wait_time = 15 * (attempt + 1)
                last_error = f"429 Too Many Requests (pokus {attempt + 1}/{max_retries})"
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    continue
                else:
                    return {"strategy": strategy, "error": last_error}

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

        except requests.exceptions.HTTPError as e:
            return {"strategy": strategy, "error": f"HTTP chyba: {e}"}
        except Exception as e:
            last_error = str(e)

    return {"strategy": strategy, "error": last_error or "Neznáma chyba"}


def run_audit_for_url(url, pagespeed_key=None, run_desktop=False, delay_between_calls=5, use_playwright=True):
    """Kompletný audit jednej URL – renderovanie stránky, SEO/accessibility/security analýza, PageSpeed."""
    html = None
    own_screenshot = None
    accessibility_issues = []
    response_headers = {}
    render_method = "requests"  # pre transparentnosť v reporte, ktorou metódou sa stránka načítala
    playwright_error = None

    if use_playwright and PLAYWRIGHT_AVAILABLE:
        rendered = fetch_rendered_page(url)
        if rendered and "error" not in rendered:
            html = rendered["html"]
            own_screenshot = rendered.get("screenshot_bytes")
            accessibility_issues = rendered.get("accessibility_issues", [])
            response_headers = rendered.get("headers", {})
            render_method = "playwright"
        elif rendered:
            playwright_error = rendered.get("error")
    elif use_playwright and not PLAYWRIGHT_AVAILABLE:
        playwright_error = "Playwright balíček nie je v tomto prostredí nainštalovaný."

    if html is None:
        # Fallback – buď Playwright nie je dostupný, alebo zlyhal na tejto URL
        html, response_headers = fetch_html(url)
        if html is None:
            return {"url": url, "error": "Nepodarilo sa stiahnuť ani vyrenderovať stránku"}

    seo_data = analyze_html(url, html)
    security_checks = check_security_headers(response_headers)
    security_checks["https"] = url.strip().lower().startswith("https://")

    ps_mobile = get_pagespeed_data(url, "mobile", api_key=pagespeed_key)
    time.sleep(delay_between_calls)  # pauza medzi PageSpeed requestami kvôli rate limitu

    ps_desktop = None
    if run_desktop:
        ps_desktop = get_pagespeed_data(url, "desktop", api_key=pagespeed_key)
        time.sleep(delay_between_calls)

    result = {
        "seo": seo_data,
        "render_method": render_method,
        "playwright_error": playwright_error,
        "accessibility_issues": accessibility_issues,
        "security_checks": security_checks,
        "own_screenshot_bytes": own_screenshot,
        "pagespeed_mobile": ps_mobile,
    }
    if ps_desktop is not None:
        result["pagespeed_desktop"] = ps_desktop
    return result


def generate_gemini_report(gemini_key, all_results, homepage_url, model_name="gemini-3.5-flash"):
    """Vygeneruje audit report cez Gemini."""
    clean_key = gemini_key.strip() if gemini_key else ""
    if not clean_key:
        raise ValueError("Gemini API kľúč je prázdny.")

    genai.configure(api_key=clean_key)

    # Odstráň screenshot base64 z promptu (príliš veľké)
    results_for_prompt = []
    for r in all_results:
        entry = dict(r)
        entry.pop("own_screenshot_bytes", None)  # bytes nie sú JSON-serializovateľné
        for variant in ["pagespeed_mobile", "pagespeed_desktop"]:
            if variant in entry and isinstance(entry[variant], dict) and entry[variant].get("screenshot_base64"):
                entry[variant] = {
                    k: v for k, v in entry[variant].items() if k != "screenshot_base64"
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

## ♿ Prístupnosť (accessibility_issues)
Pre každú stránku vypíš nájdené axe-core violácie (impact, popis, počet dotknutých prvkov) a konkrétny návrh opravy.

## 🛡️ Bezpečnosť (security_checks)
Vyhodnoť HTTPS a bezpečnostné HTTP hlavičky pre každú stránku, ktoré chýbajú a prečo sú dôležité.

## 🛒 Konverzné odporúčania
- Analýza formulárov a vyhľadávania
- CTA prvky
- Nákupný proces
- Trust signály

## ✅ Akčný plán
Zoraď odporúčania podľa priority (vysoká/stredná/nízka) a odhadovaného dopadu.
Použi formát tabuľky: | Priorita | Odporúčanie | Dopad | Náročnosť |

Buď konkrétny, uvádzaj presné hodnoty z dát. Nepoužívaj všeobecné frázy."""

    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        err_msg = str(e)
        if "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
            raise RuntimeError(
                "Gemini API kľúč nie je platný. Skontroluj, prosím:\n"
                "1) Že si kľúč skopíroval celý, bez medzery na začiatku/konci\n"
                "2) Že kľúč je vytvorený na https://aistudio.google.com/apikey (nie Cloud Console kľúč "
                "s obmedzeniami, ktorý blokuje server-side volania)\n"
                "3) Že v Google Cloud projekte je povolené 'Generative Language API'\n"
                "4) Že kľúč nemá nastavené HTTP referrer restrictions (tie blokujú volania zo Streamlit servera)"
            ) from e
        if "404" in err_msg or "is no longer available" in err_msg or "not found" in err_msg.lower():
            raise RuntimeError(
                f"Model '{model_name}' už nie je dostupný (Google ho vypol). "
                "Skús namiesto neho alias 'gemini-flash-latest' (aktualizuje sa automaticky), "
                "alebo si over aktuálny zoznam na https://ai.google.dev/gemini-api/docs/models "
                "a zmeň názov modelu v poli '🤖 Gemini model' vyššie."
            ) from e
        raise


# ── Hlavná logika ──

if st.button("🔍 Spustiť audit", type="primary", use_container_width=True):
    if not gemini_key or not gemini_key.strip():
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

    urls = urls[: max_pages + 1]  # +1 pre homepage, zvyšok podľa voľby v UI

    st.info(
        f"📄 Auditujem {len(urls)} stránok "
        f"(medzi PageSpeed requestami je 5s pauza, pri 429 chybe sa čaká a skúša znova)..."
    )

    all_results = []
    progress_bar = st.progress(0)
    status_placeholder = st.empty()

    for i, url in enumerate(urls):
        status_placeholder.write(f"⏳ Analyzujem: `{url}`")
        result = run_audit_for_url(
            url,
            pagespeed_key=pagespeed_key if pagespeed_key else None,
            run_desktop=run_desktop_too,
            use_playwright=use_playwright_render,
        )
        all_results.append(result)
        progress_bar.progress((i + 1) / len(urls))

    progress_bar.empty()
    status_placeholder.empty()

    # ── Screenshoty stránok ──
    st.subheader("📸 Screenshoty stránok")

    for result in all_results:
        if "error" in result:
            continue
        url = result["seo"]["url"]

        # Vlastný screenshot z Playwright – vidí aj JS-rendered obsah, nezávisí od PageSpeed API
        own_shot = result.get("own_screenshot_bytes")
        if own_shot:
            try:
                st.markdown(f"**🖥️ Live screenshot (renderovaný prehliadačom): {url}**")
                st.image(Image.open(BytesIO(own_shot)), use_container_width=True)
            except Exception:
                pass

        for variant in ["pagespeed_mobile", "pagespeed_desktop"]:
            ps = result.get(variant)
            if not ps:
                continue
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

        st.markdown(f"**🖼️ Náhľad: {url}**")
        st.image(get_screenshot_url(url), use_container_width=True)

    # ── PageSpeed skóre ──
    st.subheader("⚡ PageSpeed skóre")
    for result in all_results:
        if "error" in result:
            st.warning(f"❌ {result.get('url', 'N/A')}: {result['error']}")
            continue
        url = result["seo"]["url"]

        variants = [("pagespeed_mobile", "📱 Mobile")]
        if run_desktop_too:
            variants.append(("pagespeed_desktop", "🖥️ Desktop"))

        cols = st.columns(len(variants))
        for col, (variant, label) in zip(cols, variants):
            ps = result.get(variant, {}) or {}
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

    # ── Accessibility a Security ──
    st.subheader("♿ Prístupnosť a 🛡️ Bezpečnosť")
    for result in all_results:
        if "error" in result:
            continue
        url = result["seo"]["url"]
        col1, col2 = st.columns(2)

        with col1:
            st.markdown(f"**♿ Accessibility: {url}**")
            issues = result.get("accessibility_issues", [])
            if not issues:
                if result.get("render_method") == "playwright":
                    st.success("Žiadne axe-core violácie nenájdené.")
                elif result.get("playwright_error"):
                    st.error(f"Playwright zlyhal: {result['playwright_error']}")
                    st.caption("Použil sa fallback (obyčajný HTTP request) – accessibility kontrola nebola spustená.")
                else:
                    st.caption("Accessibility kontrola vyžaduje Playwright renderovanie (bolo vypnuté/nedostupné).")
            else:
                for iss in issues[:8]:
                    impact = iss.get("impact", "?")
                    icon = {"critical": "🔴", "serious": "🟠", "moderate": "🟡", "minor": "⚪"}.get(impact, "⚪")
                    st.markdown(f"{icon} **{iss.get('help')}** – dotknutých prvkov: {iss.get('nodes_affected')}")

        with col2:
            st.markdown(f"**🛡️ Security headers: {url}**")
            sec = result.get("security_checks", {})
            labels = {
                "https": "HTTPS",
                "strict_transport_security": "Strict-Transport-Security",
                "content_security_policy": "Content-Security-Policy",
                "x_content_type_options": "X-Content-Type-Options",
                "x_frame_options": "X-Frame-Options",
                "referrer_policy": "Referrer-Policy",
            }
            for key, label in labels.items():
                ok = sec.get(key)
                st.markdown(f"{'✅' if ok else '❌'} {label}")

    # ── AI Report ──
    st.subheader("📝 Generujem AI audit report...")
    with st.spinner("Gemini analyzuje dáta a píše report..."):
        try:
            report = generate_gemini_report(gemini_key, all_results, homepage_url, model_name=gemini_model_name)
            st.markdown("---")
            st.markdown(report)

            dl_col1, dl_col2 = st.columns(2)
            with dl_col1:
                st.download_button(
                    label="📥 Stiahnuť report ako TXT",
                    data=report,
                    file_name="eshop_audit_report.txt",
                    mime="text/plain",
                )
            with dl_col2:
                try:
                    pdf_bytes = convert_report_to_pdf(report, homepage_url)
                    st.download_button(
                        label="📄 Stiahnuť report ako PDF",
                        data=pdf_bytes,
                        file_name="eshop_audit_report.pdf",
                        mime="application/pdf",
                    )
                except Exception as pdf_err:
                    st.warning(f"PDF export zlyhal ({pdf_err}), použi prosím TXT verziu.")
        except Exception as e:
            st.error(f"Chyba pri generovaní reportu: {e}")
