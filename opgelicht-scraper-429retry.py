"""
Scraper voor opgelicht.nl - Alerts (RETRY VAN 429's UIT run_log.csv van run 1)
==================================================================================
Doel: per alert ophalen welke platforms/bedrijven worden genoemd,
samen met datum en categorie, t.b.v. analyse in Kibana.

ONDERZOEKSSCOPE: alleen alerts gepubliceerd tussen 2022-01-01 en 2025-12-31.

DEZE VARIANT: ALLEEN 429-URLs UIT EEN EERDERE RUN OPNIEUW PROBEREN
  Dit is een aangepaste versie van de oorspronkelijke scraper, bedoeld
  om NA een volledige run specifiek de URLs opnieuw te scrapen die toen
  een HTTP 429 (Too Many Requests) gaven.

  In plaats van stap 1 (de volledige Wayback CDX-bevraging, die alle
  ~7000 historische URLs ophaalt) wordt nu RUN_LOG_INPUT_FILE
  (run_log.csv) ingelezen. Daarin staat een 'fouten'-kolom met alle
  mislukte URLs van de vorige run, in de vorm 'url -> foutmelding',
  gescheiden door ' | '. ALLEEN de regels waar
  '429' in de foutmelding voorkomt worden gefilterd - andere foutsoorten (404, timeout,
  connection error, etc.) worden bewust NIET opnieuw geprobeerd door
  dit script.

Auteur: Thijs (HSL)
"""

import csv
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# De 'fouten'-kolom in run_log.csv kan honderden URL+foutmelding-paren
# in één cel bevatten. Python's standaard csv-veldlimiet (131072 bytes)
# is daarvoor te klein bij een drukke run -> verhogen naar het maximum.
csv.field_size_limit(sys.maxsize)

# --- Configuratie -----------------------------------------------------------

BASE_URL = "https://opgelicht.avrotros.nl"
ALERTS_URL = f"{BASE_URL}/alerts"

# Input voor DEZE variant: het run_log.csv van (een) eerdere run(s),
# waaruit we de URLs met een 429-fout opnieuw gaan scrapen. Pas dit pad
# aan als je run_log.csv ergens anders staat (bv. in coc_output/).
RUN_LOG_INPUT_FILE = Path("run_log.csv")

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


# --- Stap 1a: 429-URLs uit run_log.csv inlezen ------------------------------

def load_429_urls_from_run_log(path: Path = RUN_LOG_INPUT_FILE) -> list[str]:
    """
    Leest run_log.csv (van een of meerdere eerdere runs) in en geeft
    alle URLs terug die destijds een HTTP 429 (Too Many Requests) gaven.

    De 'fouten'-kolom van elke rij bevat alle mislukte URLs van die run,
    in de vorm 'url -> foutmelding', onderling gescheiden door ' | '
    (zie log_run() / run_info["fouten"] in het hoofdprogramma). Per paar
    controleren we of '429' in de foutmelding voorkomt; alle andere
    foutsoorten (404, timeout, connection error, etc.) slaan we BEWUST
    over - die wil je niet via dit script opnieuw proberen.

    Als run_log.csv meerdere rijen heeft (meerdere eerdere runs), lezen
    we ze allemaal in en voegen we de gevonden 429-URLs samen (dubbelen
    worden verwijderd), zodat je dit ook na meerdere runs kunt draaien.
    """
    if not path.exists():
        print(f"    [!] {path} niet gevonden - kan geen 429-URLs inlezen")
        return []

    urls_429: set[str] = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "fouten" not in (reader.fieldnames or []):
            print(f"    [!] Kolom 'fouten' niet gevonden in {path} "
                  f"(kolommen: {reader.fieldnames})")
            return []
        for row in reader:
            fouten = row.get("fouten") or ""
            if not fouten.strip():
                continue
            for pair in fouten.split(" | "):
                if " -> " not in pair:
                    continue
                url, foutmelding = pair.split(" -> ", 1)
                if "429" in foutmelding:
                    urls_429.add(url.strip())

    print(f"    [✓] {len(urls_429)} unieke URLs met 429-fout gevonden in {path}")
    return sorted(urls_429)


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

def fetch_html(url: str) -> tuple[BeautifulSoup, str]:
    """
    Haal de HTML van een URL op en parse die met BeautifulSoup.
    Geeft ook de UITEINDELIJKE URL terug (na eventuele redirects), zodat
    we de canonieke URL kunnen opslaan i.p.v. de mogelijk-verouderde
    '/alerts/artikel/...'-vorm waarmee we begonnen.
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser"), response.url


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
    print(f"[i] Dit is de 429-RETRY variant: input komt uit "
          f"{RUN_LOG_INPUT_FILE}, niet uit Wayback.\n")

    errors = []

    seen_urls = load_seen_urls()
    print(f"[resumable] {len(seen_urls)} URLs eerder al bekeken (uit {SEEN_URLS_FILE.name})\n")

    # Stap 1: alleen de URLs die in run_log.csv een 429 gaven
    all_urls_raw = load_429_urls_from_run_log()

    # Dedupe op basis van genormaliseerde slug: voorkomt dat we straks
    # twee keer dezelfde pagina scrapen via /alerts/<slug>-<id> EN
    # /alerts/artikel/<slug> (de oude URL-structuur die server-side
    # naar de nieuwe wordt geredirect).
    all_urls_now = dedupe_urls_by_slug(all_urls_raw)
    print(f"[+] {len(all_urls_now)} unieke URLs over na slug-dedupe "
          f"(was {len(all_urls_raw)})\n")

    # GEEN seen_urls-filter hier: de 429-URLs uit run_log.csv staan
    # daar (in de oorspronkelijke run) waarschijnlijk al in, omdat het
    # origineel elke bezochte URL aan seen_urls toevoegt - ook als die
    # een 429 opleverde. Filteren op seen_urls zou dus juist precies de
    # URLs overslaan die we hier opnieuw willen proberen. We bezoeken
    # daarom gewoon ALLE URLs uit all_urls_now.
    new_urls = all_urls_now
    print(f"[+] {len(new_urls)} URLs worden opnieuw geprobeerd\n")

    if not new_urls:
        print("[i] Geen 429-URLs gevonden om opnieuw te proberen.")

    # Stap 2: per URL de live detailpagina scrapen, met datumfilter.
    # scrape_alert_page() geeft None terug bij buiten-scope of ontbrekende
    # datum - die URLs zijn 'definitief klaar' en gaan WEL in seen_urls.json.
    #
    # Geeft een URL HIER WEER een 429, dan zetten we 'm BEWUST NIET in
    # seen_urls.json: de fout komt (net als in het origineel) terecht in
    # run_info["fouten"] van run_log.csv, en je kunt dit script dus
    # gewoon nog een keer draaien om die URL opnieuw te proberen.
    results = []
    skipped_out_of_scope = 0
    bezochte_urls = []
    opnieuw_429 = 0
    for i, url in enumerate(new_urls, 1):
        print(f"[{i}/{len(new_urls)}] Scrapen: {url}")
        try:
            record = scrape_alert_page(url)
            bezochte_urls.append(url)
            if record is None:
                skipped_out_of_scope += 1
            else:
                results.append(record)
        except Exception as e:
            error_text = str(e)
            if "429" in error_text:
                print(f"    [!] Wéér een 429 — NIET in seen_urls.json gezet, "
                      f"opnieuw proberen mogelijk via volgende run_log.csv: {e}")
                opnieuw_429 += 1
            else:
                print(f"    [!] Fout (definitief, wordt niet opnieuw geprobeerd): {e}")
                bezochte_urls.append(url)
            errors.append(f"{url} -> {e}")
        time.sleep(random.uniform(1.0, 2.0))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[✓] Klaar: {len(results)} alerts binnen scope opgeslagen in '{OUTPUT_FILE}'")
    print(f"    {skipped_out_of_scope} URLs overgeslagen (buiten scope of geen datum)")
    print(f"    {opnieuw_429} URLs gaven WÉÉR een 429 — niet in seen_urls.json, "
          f"draai dit script opnieuw op basis van de nieuwe run_log.csv")

    seen_urls.update(bezochte_urls)
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
        "input_run_log": str(RUN_LOG_INPUT_FILE),
        "aantal_429_urls_uit_run_log": len(all_urls_now),
        "aantal_urls_geprobeerd": len(new_urls),
        "aantal_records_binnen_scope": len(results),
        "aantal_overgeslagen_buiten_scope": skipped_out_of_scope,
        "aantal_opnieuw_429": opnieuw_429,
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
