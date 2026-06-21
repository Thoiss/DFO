"""

Auteur: Thijs (HSL)
"""

import csv
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Configuratie -----------------------------------------------------------

BASE_URL = "https://opgelicht.avrotros.nl"
ALERTS_URL = f"{BASE_URL}/alerts"

# Wayback CDX Server API - gratis, geen API-key nodig.
# We vragen alle gearchiveerde URLs op die beginnen met /alerts/
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"

# Onderzoeksscope: alleen artikelen met een publicatiedatum binnen dit
# bereik komen in de dataset. Artikelen erbuiten (bv. 2026) worden
# tijdens het scrapen overgeslagen - zie scrape_alert_page().
SCOPE_START = datetime(2022, 1, 1, tzinfo=timezone.utc)
SCOPE_END = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

# User-Agent voor de requests-calls (Wayback CDX API + live detailpagina's).
# We doen ons voor als een normale browser; chain of custody borgen we via
# dit logbestand en GitHub-commits van de scraper-code zelf.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# --- CoC: outputmap structuur -----------------------------------------------

OUTPUT_DIR = Path("coc_output")
OUTPUT_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")  # bv. 20260620_143200
OUTPUT_FILE = OUTPUT_DIR / f"opgelicht_alerts_{RUN_ID}.json"
HASH_FILE = OUTPUT_DIR / f"opgelicht_alerts_{RUN_ID}.sha256"
RUN_LOG_FILE = OUTPUT_DIR / "run_log.csv"

# Resumable scraping: dit bestand onthoudt over ALLE runs heen welke
# URLs al gescraped zijn. Geen RUN_ID in de naam, want dit bestand
# blijft bestaan en groeit met elke run.
SEEN_URLS_FILE = OUTPUT_DIR / "seen_urls.json"


# --- CoC: hulpfuncties voor hashing en logging ------------------------------

def sha256_of_file(path: Path) -> str:
    """
    Berekent de SHA-256 hash van een bestand in blokken (geheugenvriendelijk,
    ook bij grote datasets). Deze hash bewijst dat het bestand na het
    scrapen niet (per ongeluk of opzettelijk) is aangepast.
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            sha256.update(block)
    return sha256.hexdigest()


def log_run(run_info: dict) -> None:
    """
    Schrijft één regel toe aan run_log.csv (doorlopend logbestand).
    Dit bestand kun je direct gebruiken om je Word CoC-logboek mee te vullen.
    """
    file_exists = RUN_LOG_FILE.exists()
    with open(RUN_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(run_info.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(run_info)


# --- Resumable: hulpfuncties voor seen_urls.json ----------------------------

def load_seen_urls() -> set[str]:
    """Leest seen_urls.json in; lege set als het bestand nog niet bestaat."""
    if not SEEN_URLS_FILE.exists():
        return set()
    with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(seen_urls: set[str]) -> None:
    """Schrijft de (bijgewerkte) set van geziene URLs terug naar schijf."""
    with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, ensure_ascii=False, indent=2)


# --- Stap 1a: Wayback CDX API - historische URLs verzamelen ----------------

def collect_urls_from_wayback() -> list[str]:
    """
    Vraagt de Wayback Machine CDX API om alle gearchiveerde URLs die
    beginnen met opgelicht.avrotros.nl/alerts/.

    BELANGRIJK: de 'timestamp' die deze API teruggeeft is het moment
    waarop het Internet Archive de pagina heeft gecrawld/gefotografeerd -
    NIET de publicatiedatum van het artikel. We gebruiken die timestamp
    dan ook niet; we gebruiken ALLEEN de 'original' URL. De echte
    publicatiedatum wordt later, in scrape_alert_page(), uit de live
    pagina gehaald (meta-tag 'article:published_time'). Dat is ook waar
    de daadwerkelijke scope-filtering (2022-2025) plaatsvindt.

    We vragen daarom alleen 'original' (de live URL) op, en gebruiken
    collapse=urlkey zodat elke unieke URL maar één keer terugkomt (een
    pagina kan meerdere keren gearchiveerd zijn, met verschillende
    timestamps die voor ons doel irrelevant zijn).
    """
    print("[+] Wayback CDX API bevragen voor historische alert-URLs...")
    params = {
        "url": f"{BASE_URL.replace('https://', '')}/alerts/*",
        "output": "json",
        "fl": "original,timestamp",
        "collapse": "urlkey",
    }
    try:
        response = requests.get(
            WAYBACK_CDX_URL, params=params, headers=HEADERS, timeout=60
        )
        response.raise_for_status()
        rows = response.json()
    except Exception as e:
        print(f"    [!] Wayback CDX API-aanroep mislukt: {e}")
        return []

    if not rows or len(rows) < 2:
        print("    [!] Geen resultaten van Wayback CDX API")
        return []

    # Eerste rij is de header (kolomnamen), de rest zijn de data
    header, *data_rows = rows
    url_index = header.index("original")

    urls = set()
    for row in data_rows:
        url = row[url_index]
        # Alleen daadwerkelijke alert-detailpagina's, niet /alerts zelf
        if "/alerts/" in url and not url.rstrip("/").endswith("/alerts"):
            # Wayback bewaart soms http:// en https:// varianten, en
            # soms met/zonder trailing slash -> normaliseren naar https.
            normalized = url.replace("http://", "https://").rstrip("/")
            urls.add(normalized)

    print(f"    [✓] {len(urls)} unieke historische alert-URLs gevonden via Wayback")
    return sorted(urls)


def normalize_slug(url: str) -> str:
    """
    Normaliseert een alert-URL naar een vergelijkbare 'kale' slug, zodat
    we duplicaten kunnen herkennen tussen de oude URL-structuur
    (/alerts/artikel/<slug>) en de nieuwe (/alerts/<slug>-<id>).

    Voorbeeld:
      /alerts/aantal-valse-mails-is-vervijfvoudigd-11302   -> aantal-valse-mails-is-vervijfvoudigd
      /alerts/artikel/aantal-valse-mails-is-vervijfvoudigd -> aantal-valse-mails-is-vervijfvoudigd

    Let op: dit is een BESTE-POGING normalisatie op tekst, geen garantie.
    Sommige oude/nieuwe slug-paren verschillen net iets in spelling (bv.
    '...voor-40000-opgelicht' vs '...voor-eur40000-opgelicht'), en die
    worden hier NIET als duplicaat herkend. Die gevallen vangen we pas
    af bij het scrapen zelf, via de uiteindelijke redirect-URL
    (zie scrape_alert_page()).
    """
    path = url.split("/alerts/", 1)[-1]
    if path.startswith("artikel/"):
        path = path[len("artikel/"):]
    # Verwijder een numeriek ID-suffix aan het einde, bv. '-11302'
    path = re.sub(r"-\d+$", "", path)
    return path.rstrip("/")


def dedupe_urls_by_slug(urls: list[str]) -> list[str]:
    """
    Filtert een lijst van URLs zodat per genormaliseerde slug (zie
    normalize_slug()) maar één URL overblijft. Bij een duplicaat
    geven we voorkeur aan de NIEUWE URL-vorm (/alerts/<slug>-<id>,
    dus zonder '/artikel/'), omdat die direct (zonder redirect) laadt.

    Dit voorkomt dat we straks twee keer dezelfde pagina-inhoud
    scrapen (één keer via een omleiding) voor exact hetzelfde artikel.
    """
    best_url_per_slug: dict[str, str] = {}
    for url in urls:
        slug = normalize_slug(url)
        is_old_style = "/alerts/artikel/" in url
        if slug not in best_url_per_slug:
            best_url_per_slug[slug] = url
        elif is_old_style is False:
            # Nieuwe URL-vorm heeft voorrang over een eerder gevonden
            # oude (/artikel/) vorm voor dezelfde slug.
            best_url_per_slug[slug] = url

    deduped = sorted(best_url_per_slug.values())
    removed = len(urls) - len(deduped)
    if removed > 0:
        print(f"    [i] {removed} vermoedelijke duplicaten verwijderd "
              f"op basis van genormaliseerde slug")
    return deduped


# --- Stap 2: BeautifulSoup - elke detailpagina parsen -----------------------

def fetch_html(url: str, max_retries: int = 3) -> tuple[BeautifulSoup, str]:
    """
    Haal de HTML van een URL op en parse die met BeautifulSoup.
    Geeft ook de UITEINDELIJKE URL terug (na eventuele redirects), zodat
    we de canonieke URL kunnen opslaan i.p.v. de mogelijk-verouderde
    '/alerts/artikel/...'-vorm waarmee we begonnen.

    Bij HTTP 429 (Too Many Requests) wordt de 'Retry-After' header van
    de server gerespecteerd (de server geeft hiermee zelf aan hoe lang
    je moet wachten). Is die header niet aanwezig, dan wachten we
    oplopend langer per poging (exponential backoff: 10s, 20s, 40s).
    Na max_retries mislukte pogingen geven we de fout door aan de
    aanroeper, die de URL dan als 'tijdelijk mislukt' registreert
    (zie main()) zodat een volgende run het opnieuw probeert.
    """
    for attempt in range(1, max_retries + 1):
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = 10 * (2 ** (attempt - 1))  # 10s, 20s, 40s
            print(f"    [!] 429 Too Many Requests (poging {attempt}/{max_retries}), "
                  f"{wait}s wachten...")
            time.sleep(wait)
            continue

        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser"), response.url

    # Alle retries op 429 uitgeput -> alsnog de fout opwerpen, zodat
    # main() dit als tijdelijke fout (niet als 'definitief klaar') ziet.
    response.raise_for_status()


def parse_published_date(soup: BeautifulSoup) -> datetime | None:
    """
    Leest de publicatiedatum uit de 'article:published_time' meta-tag
    en zet die om naar een timezone-aware datetime, zodat we 'm kunnen
    vergelijken met SCOPE_START / SCOPE_END.
    """
    date_meta = soup.find("meta", attrs={"property": "article:published_time"})
    if not date_meta or not date_meta.get("content"):
        return None
    raw = date_meta["content"]
    try:
        # Formaat: '2026-04-15T15:51:13.467Z' -> ISO 8601 met Z voor UTC.
        # Python's fromisoformat kent 'Z' niet rechtstreeks, dus vervangen
        # we die door '+00:00'.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def scrape_alert_page(url: str) -> dict | None:
    """
    Scrape één alert-detailpagina en geef een gestructureerde dict terug.

    Geeft None terug (en scraped dus NIETS) als de publicatiedatum
    buiten de onderzoeksscope (SCOPE_START - SCOPE_END) valt, of als
    er geen publicatiedatum te vinden is. Dit voorkomt dat 2026-artikelen
    (buiten scope) alsnog in de dataset terechtkomen.
    """
    soup, final_url = fetch_html(url)

    datum_dt = parse_published_date(soup)
    if datum_dt is None:
        print(f"    [!] Geen publicatiedatum gevonden — overgeslagen")
        return None
    if not (SCOPE_START <= datum_dt <= SCOPE_END):
        print(f"    [-] Buiten scope ({datum_dt.date()}) — overgeslagen")
        return None

    datum = datum_dt.isoformat()

    h1 = soup.find("h1")
    titel = h1.get_text(strip=True) if h1 else None

    desc_meta = soup.find("meta", attrs={"name": "description"})
    beschrijving = desc_meta["content"] if desc_meta else None

    onderwerpen = []
    for a in soup.find_all("a", href=True):
        if "/onderwerpen/" in a["href"]:
            tekst = a.get_text(strip=True)
            if tekst and tekst not in onderwerpen:
                onderwerpen.append(tekst)

    tags = []
    for a in soup.find_all("a", href=True):
        if "/tags/" in a["href"]:
            tekst = a.get_text(strip=True)
            if tekst and tekst not in tags:
                tags.append(tekst)

    return {
        "url": final_url.rstrip("/"),
        "titel": titel,
        "datum": datum,
        "beschrijving": beschrijving,
        "onderwerpen": onderwerpen,
        "tags": tags,
    }


# --- Hoofdprogramma ----------------------------------------------------------

def main():
    start_time = datetime.now(timezone.utc).astimezone()
    print(f"[CoC] Run gestart: {start_time.isoformat()} (RUN_ID={RUN_ID})\n")

    errors = []

    seen_urls = load_seen_urls()
    print(f"[resumable] {len(seen_urls)} URLs eerder al bekeken (uit {SEEN_URLS_FILE.name})\n")

    # Stap 1: alle historische URLs via Wayback
    all_urls_raw = collect_urls_from_wayback()

    # Dedupe op basis van genormaliseerde slug: voorkomt dat we straks
    # twee keer dezelfde pagina scrapen via /alerts/<slug>-<id> EN
    # /alerts/artikel/<slug> (de oude URL-structuur die server-side
    # naar de nieuwe wordt geredirect).
    all_urls_now = dedupe_urls_by_slug(all_urls_raw)
    print(f"[+] {len(all_urls_now)} unieke URLs over na slug-dedupe "
          f"(was {len(all_urls_raw)})\n")

    # Resumable: alleen nieuwe URLs daadwerkelijk bezoeken
    new_urls = [u for u in all_urls_now if u not in seen_urls]
    print(f"[resumable] Daarvan zijn {len(new_urls)} URLs nieuw t.o.v. vorige runs\n")

    if not new_urls:
        print("[i] Geen nieuwe alerts gevonden.")

    # Stap 2: per nieuwe URL de live detailpagina scrapen, met datumfilter.
    # scrape_alert_page() geeft None terug bij buiten-scope of ontbrekende
    # datum. We onderscheiden twee soorten 'mislukt':
    #   - DEFINITIEF (404, geen datum, buiten scope): de URL gaat WEL in
    #     seen_urls.json, want een volgende run hoeft dit niet opnieuw
    #     te proberen - het antwoord verandert niet.
    #   - TIJDELIJK (429 Too Many Requests, timeout, verbindingsfout): de
    #     URL gaat NIET in seen_urls.json, zodat een volgende run hem
    #     automatisch opnieuw oppakt.
    results = []
    skipped_out_of_scope = 0
    definitief_afgehandeld = []   # -> wel in seen_urls.json
    tijdelijk_mislukt = []        # -> niet in seen_urls.json, opnieuw proberen

    # Foutmeldingen waarvan we weten dat herhalen waarschijnlijk weer
    # lukt na een tijdje (rate limiting, server tijdelijk overbelast,
    # netwerk-hikje) - deze URLs NIET als definitief markeren.
    TIJDELIJKE_FOUT_SIGNALEN = ("429", "503", "Timeout", "ConnectionError")

    for i, url in enumerate(new_urls, 1):
        print(f"[{i}/{len(new_urls)}] Scrapen: {url}")
        try:
            record = scrape_alert_page(url)
            definitief_afgehandeld.append(url)
            if record is None:
                skipped_out_of_scope += 1
            else:
                results.append(record)
        except Exception as e:
            error_text = str(e)
            is_tijdelijk = any(signaal in error_text for signaal in TIJDELIJKE_FOUT_SIGNALEN)
            if is_tijdelijk:
                print(f"    [!] Tijdelijke fout (volgende run opnieuw proberen): {e}")
                tijdelijk_mislukt.append(url)
            else:
                print(f"    [!] Definitieve fout: {e}")
                definitief_afgehandeld.append(url)
            errors.append(f"{url} -> {e}")
        time.sleep(random.uniform(1.0, 2.0))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[✓] Klaar: {len(results)} nieuwe alerts binnen scope opgeslagen in '{OUTPUT_FILE}'")
    print(f"    {skipped_out_of_scope} URLs overgeslagen (buiten scope of geen datum)")
    print(f"    {len(tijdelijk_mislukt)} URLs tijdelijk mislukt — worden bij volgende run opnieuw geprobeerd")

    # Alleen definitief afgehandelde URLs gaan in seen_urls.json.
    # Tijdelijk mislukte URLs blijven 'onbekend', zodat de volgende
    # keer dat je dit script draait ze automatisch opnieuw worden
    # opgepakt door de resumable-logica (new_urls-filter hierboven).
    seen_urls.update(definitief_afgehandeld)
    save_seen_urls(seen_urls)
    print(f"[resumable] {SEEN_URLS_FILE.name} bijgewerkt — totaal nu {len(seen_urls)} URLs bekend")

    file_hash = sha256_of_file(OUTPUT_FILE)
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(f"{file_hash}  {OUTPUT_FILE.name}\n")
    print(f"[CoC] SHA-256: {file_hash}")

    end_time = datetime.now(timezone.utc).astimezone()

    run_info = {
        "run_id": RUN_ID,
        "start_tijd": start_time.isoformat(timespec="seconds"),
        "eind_tijd": end_time.isoformat(timespec="seconds"),
        "duur_seconden": round((end_time - start_time).total_seconds()),
        "wayback_urls_totaal": len(all_urls_now),
        "aantal_urls_nieuw_bezocht": len(new_urls),
        "aantal_records_binnen_scope": len(results),
        "aantal_overgeslagen_buiten_scope": skipped_out_of_scope,
        "aantal_tijdelijk_mislukt": len(tijdelijk_mislukt),
        "aantal_errors": len(errors),
        "output_bestand": OUTPUT_FILE.name,
        "sha256": file_hash,
        "user_agent": HEADERS["User-Agent"],
        "fouten": " | ".join(errors) if errors else "",
    }
    log_run(run_info)
    print(f"[CoC] Run gelogd in: {RUN_LOG_FILE}")

    print("\n--- Samenvatting voor je CoC-logboek (Word document) ---")
    for key, value in run_info.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
