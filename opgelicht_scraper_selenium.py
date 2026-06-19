"""
Scraper voor opgelicht.nl - Alerts overzicht (Selenium-versie)
===============================================================
Doel: per alert ophalen welke platforms/bedrijven worden genoemd,
samen met datum en categorie, t.b.v. analyse in Kibana.

Hybride aanpak:
  - Selenium opent een echte browser, klikt op 'Toon meer' om de
    volledige geschiedenis aan alerts te laden, en verzamelt alle URLs.
  - BeautifulSoup + requests bezoeken vervolgens elke URL afzonderlijk
    om de details (datum, tags, ...) op te halen. Dit is veel sneller
    dan voor elke detailpagina opnieuw een browser te openen.

Auteur: Thijs (HSL)
"""

import random
import time
import json

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException,
    ElementClickInterceptedException,
)

# --- Configuratie -----------------------------------------------------------

BASE_URL = "https://opgelicht.avrotros.nl"
ALERTS_URL = f"{BASE_URL}/alerts"

# Hoe vaak we maximaal op 'Toon meer' drukken.
# Begin laag (bv. 5) voor een test; verhoog later naar bv. 500 voor de
# volledige geschiedenis 2022-2025.
MAX_CLICKS = 5

# Browser zichtbaar (False) of onzichtbaar (True). Voor de eerste run
# False laten zodat je kunt zien wat er gebeurt en evt. cookies wegklikt.
HEADLESS = False

# User-Agent voor de requests-call op detailpagina's (we doen ons voor
# als een normale browser; chain of custody borgen we via GitHub-log).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

OUTPUT_FILE = "opgelicht_alerts.json"


# --- Stap 1: Selenium - alle alert-URLs verzamelen --------------------------

def collect_alert_urls_selenium() -> list[str]:
    """
    Open de overzichtspagina in een echte browser, druk herhaaldelijk op
    'Toon meer' totdat de knop verdwijnt of het maximum is bereikt,
    en verzamel alle gevonden alert-URLs.
    """
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)

    try:
        print(f"[+] Browser opent {ALERTS_URL}")
        driver.get(ALERTS_URL)
        time.sleep(2)  # initiële laadtijd

        for i in range(1, MAX_CLICKS + 1):
            # Tel hoeveel alert-links er momenteel zichtbaar zijn
            before = len(driver.find_elements(
                By.XPATH, "//a[contains(@href, '/alerts/')]"
            ))
            print(f"[{i}/{MAX_CLICKS}] Zichtbaar: {before} alert-links — zoeken naar 'Toon meer'")

            try:
                # De knop staat onderaan de lijst; we zoeken op tekstinhoud.
                button = driver.find_element(
                    By.XPATH, "//*[normalize-space(text())='Toon meer']"
                )
                # Scroll de knop in beeld voordat we klikken (anders kan
                # een sticky header de klik tegenhouden).
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                time.sleep(0.5)
                button.click()
            except NoSuchElementException:
                print("    [✓] Geen 'Toon meer' knop meer — alle alerts geladen")
                break
            except ElementClickInterceptedException:
                # Soms zit een cookie-banner ervoor; in dat geval handmatig
                # wegklikken in de zichtbare browser en het script gaat verder
                print("    [!] Klik geblokkeerd (cookie-banner?). Probeer handmatig weg te klikken.")
                time.sleep(5)
                continue

            # Wacht tot nieuwe items zijn ingeladen
            time.sleep(random.uniform(1.5, 2.5))

            after = len(driver.find_elements(
                By.XPATH, "//a[contains(@href, '/alerts/')]"
            ))
            if after == before:
                print("    [✓] Geen nieuwe alerts meer ingeladen — stoppen")
                break

        # Pak de finale HTML van de pagina en haal er alle alert-URLs uit
        soup = BeautifulSoup(driver.page_source, "html.parser")
        urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/alerts/" in href and href.rstrip("/") != "/alerts":
                full = href if href.startswith("http") else BASE_URL + href
                urls.add(full)
        return sorted(urls)

    finally:
        driver.quit()


# --- Stap 2: BeautifulSoup - elke detailpagina parsen -----------------------

def fetch_html(url: str) -> BeautifulSoup:
    """Haal de HTML van een URL op en parse die met BeautifulSoup."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def scrape_alert_page(url: str) -> dict:
    """Scrape één alert-detailpagina en geef een gestructureerde dict terug."""
    soup = fetch_html(url)

    date_meta = soup.find("meta", attrs={"property": "article:published_time"})
    datum = date_meta["content"] if date_meta else None

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
        "url": url,
        "titel": titel,
        "datum": datum,
        "beschrijving": beschrijving,
        "onderwerpen": onderwerpen,
        "tags": tags,
    }


# --- Hoofdprogramma ---------------------------------------------------------

def main():
    # Stap 1: alle URLs verzamelen via de echte browser
    alert_urls = collect_alert_urls_selenium()
    print(f"\n[+] Totaal {len(alert_urls)} unieke alert-URLs verzameld\n")

    # Stap 2: per URL de detailpagina scrapen
    results = []
    for i, url in enumerate(alert_urls, 1):
        print(f"[{i}/{len(alert_urls)}] Scrapen: {url}")
        try:
            results.append(scrape_alert_page(url))
        except Exception as e:
            print(f"    [!] Fout: {e}")
        time.sleep(random.uniform(1.0, 2.0))

    # Opslaan
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[✓] Klaar: {len(results)} alerts opgeslagen in '{OUTPUT_FILE}'")


if __name__ == "__main__":
    main()
