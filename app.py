import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import json
import base64
import time
from io import BytesIO
from PIL import Image
import google.generativeai as genai

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# JS knižnica na kontrolu prístupnosti (WCAG), nahráva sa priamo do stránky v prehliadači
AXE_CORE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

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

st.subheader("📄 Podstránky na audit (max 5)")
st.caption(
    "Zadaj relatívne cesty (napr. /produkty/) alebo plné URL, každú na nový riadok. "
    "Menej stránok = menej chýb s rate limitom."
)
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


@st.cache_resource(show_spinner=False)
def get_playwright_browser():
    """
    Spustí headless Chromium raz a znovu ho použije pre všetky stránky v tomto behu.
    Ak binárka Chromium ešte nie je stiahnutá (bežné pri prvom deploy na Streamlit Cloud),
    stiahne ju automaticky – toto trvá len pri úplne prvom spustení appky.
    """
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
            )
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        else:
            raise
    return pw, browser


def fetch_rendered_page(url, timeout_ms=20000):
    """
    Načíta stránku cez skutočný prehliadač (Playwright) – vidí obsah dorenderovaný JavaScriptom.
    Vráti: html, screenshot (bytes), response headers, zoznam accessibility chýb (axe-core).
    Ak Playwright zlyhá alebo nie je dostupný, vráti None a volajúci má spraviť fallback na fetch_html.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    try:
        pw, browser = get_playwright_browser()
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
        # Počkaj chvíľu na dobehnutie JS renderovania (lazy-load, SPA), ale nečakaj donekonečna
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # stránka má nekonečný polling/websocket – to je v poriadku, ideme ďalej

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

    if use_playwright and PLAYWRIGHT_AVAILABLE:
        rendered = fetch_rendered_page(url)
        if rendered and "error" not in rendered:
            html = rendered["html"]
            own_screenshot = rendered.get("screenshot_bytes")
            accessibility_issues = rendered.get("accessibility_issues", [])
            response_headers = rendered.get("headers", {})
            render_method = "playwright"

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
        "accessibility_issues": accessibility_issues,
        "security_checks": security_checks,
        "own_screenshot_bytes": own_screenshot,
        "pagespeed_mobile": ps_mobile,
    }
    if ps_desktop is not None:
        result["pagespeed_desktop"] = ps_desktop
    return result


def generate_gemini_report(gemini_key, all_results, homepage_url):
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
        model = genai.GenerativeModel("gemini-2.0-flash")
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

    urls = urls[:6]  # max 6 stránok (homepage + 5 podstránok) – menej = menej 429 chýb

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
