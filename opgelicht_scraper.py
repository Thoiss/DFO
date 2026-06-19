"""
Scraper voor opgelicht.nl - Alerts overzicht
============================================
Doel: per alert ophalen welke platforms/bedrijven worden genoemd,
samen met datum en categorie, t.b.v. analyse in Kibana.

Auteur: Thijs (HSL)
Bibliotheek: BeautifulSoup4 + requests
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import random

# --- Configuratie -----------------------------------------------------------

BASE_URL = "https://opgelicht.avrotros.nl"
ALERTS_URL = f"{BASE_URL}/alerts"

# De site weigert 'kale' scrapers (HTTP 403). Daarom doen we ons voor als
# een normale browser. Transparantie waarborgen we via de GitHub-log
# en het onderzoeksrapport (chain of custody).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

OUTPUT_FILE = "opgelicht_alerts.json"


# --- Hulpfuncties -----------------------------------------------------------

def fetch_html(url: str) -> BeautifulSoup:
    """Haal de HTML van een URL op en parse het tot een BeautifulSoup-object."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()  # gooit een fout bij 4xx/5xx
    return BeautifulSoup(response.content, "html.parser")


def get_alert_links(overview_soup: BeautifulSoup) -> list[str]:
    """
    Vind alle links naar individuele alert-detailpagina's op de overzichtspagina.
    Detail-URLs hebben de vorm: /alerts/<slug>-<id>
    """
    links = set()  # set voorkomt duplicaten
    for a in overview_soup.find_all("a", href=True):
        href = a["href"]
        # We willen alleen detailpagina's, niet de overzichtspagina zelf
        if "/alerts/" in href and href.rstrip("/") != "/alerts":
            full_url = href if href.startswith("http") else BASE_URL + href
            links.add(full_url)
    return sorted(links)


def scrape_alert_page(url: str) -> dict:
    """Scrape één alert-detailpagina en geef een gestructureerde dict terug."""
    soup = fetch_html(url)

    # --- Datum (uit meta-tag, zeer consistent) ---
    date_meta = soup.find("meta", attrs={"property": "article:published_time"})
    datum = date_meta["content"] if date_meta else None

    # --- Titel (eerste <h1> binnen de pagina) ---
    h1 = soup.find("h1")
    titel = h1.get_text(strip=True) if h1 else None

    # --- Beschrijving (uit meta description, kort en bondig) ---
    desc_meta = soup.find("meta", attrs={"name": "description"})
    beschrijving = desc_meta["content"] if desc_meta else None

    # --- Onderwerpen (links die /onderwerpen/ bevatten) ---
    onderwerpen = []
    for a in soup.find_all("a", href=True):
        if "/onderwerpen/" in a["href"]:
            tekst = a.get_text(strip=True)
            if tekst and tekst not in onderwerpen:
                onderwerpen.append(tekst)

    # --- Tags (links die /tags/ bevatten — bevatten platformnamen!) ---
    tags = []
    for a in soup.find_all("a", href=True):
        if "/tags/" in a["href"]:
            tekst = a.get_text(strip=True)
            if tekst and tekst not in tags:
                tags.append(tekst)

    return {
        "url": url,
        "titel": titel,
        "datum": datum,
        "beschrijving": beschrijving,
        "onderwerpen": onderwerpen,
        "tags": tags,
    }


# --- Hoofdprogramma ---------------------------------------------------------

def main():
    print(f"[+] Ophalen overzichtspagina: {ALERTS_URL}")
    overview = fetch_html(ALERTS_URL)

    alert_urls = get_alert_links(overview)
    print(f"[+] {len(alert_urls)} unieke alert-URLs gevonden")

    results = []
    for i, url in enumerate(alert_urls, 1):
        print(f"[{i}/{len(alert_urls)}] Scrapen: {url}")
        try:
            data = scrape_alert_page(url)
            results.append(data)
        except Exception as e:
            print(f"    [!] Fout bij {url}: {e}")

        # Random sleep tussen requests:
        #  - voorkomt overbelasting van de server
        #  - maakt het patroon minder voorspelbaar (forensisch principe)
        time.sleep(random.uniform(1.0, 2.5))

    # Opslaan als JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[✓] Klaar: {len(results)} alerts opgeslagen in '{OUTPUT_FILE}'")


if __name__ == "__main__":
    main()
