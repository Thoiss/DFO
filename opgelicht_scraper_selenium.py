"""
Scraper voor opgelicht.nl - Alerts overzicht (Selenium + CoC + Resumable)
===========================================================================
Doel: per alert ophalen welke platforms/bedrijven worden genoemd,
samen met datum en categorie, t.b.v. analyse in Kibana.
 
Hybride aanpak:
  - Selenium opent een echte browser, klikt op 'Toon meer' om de
    volledige geschiedenis aan alerts te laden, en verzamelt alle URLs.
  - BeautifulSoup + requests bezoeken vervolgens elke URL afzonderlijk
    om de details (datum, tags, ...) op te halen. Dit is veel sneller
    dan voor elke detailpagina opnieuw een browser te openen.
 
Chain of Custody (CoC) borging
  - Elke run krijgt een uniek run-ID en start-/eindtijd (met timezone).
  - Het outputbestand krijgt een SHA-256 hash, opgeslagen in een los
    .sha256 bestand zodat je kunt aantonen dat de data niet is gewijzigd.
  - Elke run wordt toegevoegd aan een doorlopend logbestand (run_log.csv)
    dat gebruikt wordt voor het CoC-logboek.
  - Een screenshot van de overzichtspagina wordt automatisch opgeslagen
    als forensisch bewijs van wat er op het moment van scrapen te zien was.
 
NIEUW in deze versie: Resumable scraping
  - 'seen_urls.json' onthoudt alle URLs die in eerdere runs al gescraped
    zijn. Bij elke nieuwe run wordt dit bestand eerst ingelezen.
  - Alleen het deel dat NOG NIET in seen_urls.json staat daadwerkelijk gescraped en opgeslagen.
  - Na de run wordt seen_urls.json bijgewerkt met de nieuw gescrapete URLs.
 
Auteur: Thijs (HSL)
"""
 
import csv
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
 
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
# als een normale browser; chain of custody borgen we via dit logbestand
# en GitHub-commits van de scraper-code zelf).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
 
# --- CoC: outputmap structuur -----------------------------------------------
# Alles wat met één run te maken heeft (data + hash + screenshot) krijgt
# dezelfde RUN_ID in de bestandsnaam, zodat alles bij elkaar traceerbaar is.
 
OUTPUT_DIR = Path("coc_output")
OUTPUT_DIR.mkdir(exist_ok=True)
 
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")  # bv. 20260619_143200
OUTPUT_FILE = OUTPUT_DIR / f"opgelicht_alerts_{RUN_ID}.json"
HASH_FILE = OUTPUT_DIR / f"opgelicht_alerts_{RUN_ID}.sha256"
SCREENSHOT_FILE = OUTPUT_DIR / f"screenshot_overview_{RUN_ID}.png"
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
    Dit bestand kun je direct gebruiken om je Word CoC-logboek mee te vullen:
    elke regel hier komt overeen met één "Logboek entry" in dat document.
    """
    file_exists = RUN_LOG_FILE.exists()
    with open(RUN_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(run_info.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(run_info)
 
 
def take_screenshot(driver, path: Path) -> None:
    """Slaat een screenshot van de huidige browserstaat op als bewijsmateriaal."""
    try:
        driver.save_screenshot(str(path))
        print(f"    [✓] Screenshot opgeslagen: {path}")
    except Exception as e:
        print(f"    [!] Screenshot mislukt: {e}")
 
 
# --- Resumable: hulpfuncties voor seen_urls.json ----------------------------
 
def load_seen_urls() -> set[str]:
    """
    Leest seen_urls.json in en geeft de set van al eerder gescraped URLs
    terug. Bestaat het bestand nog niet (eerste run ooit), dan is de
    set gewoon leeg.
    """
    if not SEEN_URLS_FILE.exists():
        return set()
    with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
 
 
def save_seen_urls(seen_urls: set[str]) -> None:
    """Schrijft de (bijgewerkte) set van geziene URLs terug naar schijf."""
    with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, ensure_ascii=False, indent=2)
 
 
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
 
        # CoC: screenshot van de startsituatie, vóór er geklikt wordt
        take_screenshot(driver, SCREENSHOT_FILE)
 
        for i in range(1, MAX_CLICKS + 1):
            before = len(driver.find_elements(
                By.XPATH, "//a[contains(@href, '/alerts/')]"
            ))
            print(f"[{i}/{MAX_CLICKS}] Zichtbaar: {before} alert-links — zoeken naar 'Toon meer'")
 
            try:
                button = driver.find_element(
                    By.XPATH, "//*[normalize-space(text())='Toon meer']"
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                time.sleep(0.5)
                button.click()
            except NoSuchElementException:
                print("    [✓] Geen 'Toon meer' knop meer — alle alerts geladen")
                break
            except ElementClickInterceptedException:
                print("    [!] Klik geblokkeerd (cookie-banner?). Probeer handmatig weg te klikken.")
                time.sleep(5)
                continue
 
            # CoC: onvoorspelbaarheid - random wachttijd i.p.v. vaste interval
            time.sleep(random.uniform(1.5, 2.5))
 
            after = len(driver.find_elements(
                By.XPATH, "//a[contains(@href, '/alerts/')]"
            ))
            if after == before:
                print("    [✓] Geen nieuwe alerts meer ingeladen — stoppen")
                break
 
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
 
 
# --- Hoofdprogramma ----------------------------------------------------------
 
def main():
    # CoC: starttijd vastleggen, met timezone, vóórdat er iets gebeurt
    start_time = datetime.now(timezone.utc).astimezone()
    print(f"[CoC] Run gestart: {start_time.isoformat()} (RUN_ID={RUN_ID})\n")
 
    errors = []
 
    # Resumable: eerst kijken welke URLs we in eerdere runs al hebben
    seen_urls = load_seen_urls()
    print(f"[resumable] {len(seen_urls)} URLs eerder al gescraped (uit {SEEN_URLS_FILE.name})\n")
 
    # Stap 1: alle URLs verzamelen via de echte browser (zoals altijd —
    # dit haalt simpelweg alles op wat nu op de pagina staat, inclusief
    # alerts die je al eerder had).
    all_urls_now = collect_alert_urls_selenium()
    print(f"\n[+] Totaal {len(all_urls_now)} unieke alert-URLs zichtbaar op de pagina")
 
    # Resumable: alleen de URLs die nog NIET eerder gescraped zijn, gaan
    # we deze run daadwerkelijk bezoeken met BeautifulSoup.
    new_urls = [u for u in all_urls_now if u not in seen_urls]
    print(f"[resumable] Daarvan zijn {len(new_urls)} URLs nieuw t.o.v. vorige runs\n")
 
    if not new_urls:
        print("[i] Geen nieuwe alerts gevonden. Verhoog MAX_CLICKS als je verder "
              "terug in de tijd wil, of wacht tot er nieuwe alerts zijn gepubliceerd.")
 
    # Stap 2: per nieuwe URL de detailpagina scrapen
    results = []
    for i, url in enumerate(new_urls, 1):
        print(f"[{i}/{len(new_urls)}] Scrapen: {url}")
        try:
            results.append(scrape_alert_page(url))
        except Exception as e:
            print(f"    [!] Fout: {e}")
            errors.append(f"{url} -> {e}")
        # CoC: onvoorspelbaarheid - random sleep tussen requests
        time.sleep(random.uniform(1.0, 2.0))
 
    # Opslaan van de data (alleen de nieuwe records van deze run)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[✓] Klaar: {len(results)} nieuwe alerts opgeslagen in '{OUTPUT_FILE}'")
 
    # Resumable: seen_urls.json bijwerken met de URLs die we nu hebben
    # geprobeerd te scrapen (zowel gelukt als mislukt — een URL die 503
    # gaf hoeft niet steeds opnieuw geprobeerd te worden; je kunt 'm zo
    # nodig handmatig uit seen_urls.json verwijderen om een retry te forceren).
    seen_urls.update(new_urls)
    save_seen_urls(seen_urls)
    print(f"[resumable] {SEEN_URLS_FILE.name} bijgewerkt — totaal nu {len(seen_urls)} URLs bekend")
 
    # --- CoC: hash berekenen over het ZOJUIST opgeslagen bestand ---
    file_hash = sha256_of_file(OUTPUT_FILE)
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(f"{file_hash}  {OUTPUT_FILE.name}\n")
    print(f"[CoC] SHA-256: {file_hash}")
    print(f"[CoC] Hash opgeslagen in: {HASH_FILE}")
 
    # CoC: eindtijd vastleggen
    end_time = datetime.now(timezone.utc).astimezone()
 
    # CoC: alles samenvoegen tot één logregel
    run_info = {
        "run_id": RUN_ID,
        "start_tijd": start_time.isoformat(timespec="seconds"),
        "eind_tijd": end_time.isoformat(timespec="seconds"),
        "duur_seconden": round((end_time - start_time).total_seconds()),
        "max_clicks": MAX_CLICKS,
        "aantal_urls_zichtbaar": len(all_urls_now),
        "aantal_urls_nieuw": len(new_urls),
        "aantal_records_opgeslagen": len(results),
        "aantal_errors": len(errors),
        "output_bestand": OUTPUT_FILE.name,
        "sha256": file_hash,
        "screenshot": SCREENSHOT_FILE.name,
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
 
