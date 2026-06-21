Scraper voor opgelicht.nl - Alerts (Wayback CDX + CoC + Resumable + Datumfilter)
==================================================================================
Doel: per alert ophalen welke platforms/bedrijven worden genoemd,
samen met datum en categorie, t.b.v. analyse in Kibana.

ONDERZOEKSSCOPE: alleen alerts gepubliceerd tussen 2022-01-01 en 2025-12-31.

WAAROM WAYBACK MACHINE I.P.V. DE 'TOON MEER'-KNOP
  De 'Toon meer'-knop op de /alerts overzichtspagina laadt na ~6 klikken
  geen oudere alerts meer (de site toont alleen een 'venster' van de
  meest recente ~100 alerts, die toevallig allemaal uit 2026 komen -
  dus buiten de onderzoeksscope vallen). Selenium is daarom niet meer
  nodig: alle relevante (2022-2025) alerts worden via Wayback gevonden.

  De Wayback Machine (web.archive.org) van het Internet Archive heeft
  talloze historische snapshots van deze site gemaakt. De CDX Server
  API geeft - gratis, zonder API-key, zonder login - een lijst van
  ALLE URLs die het Archive ooit onder een domein heeft vastgelegd.
  Wij gebruiken die lijst om te ontdekken welke /alerts/...-URLs
  hebben bestaan, ONGEACHT de timestamp die Wayback erbij vermeldt
  (die timestamp is het moment van archiveren, niet de publicatiedatum
  van het artikel - zie toelichting bij collect_urls_from_wayback()).

  We scrapen vervolgens NIET de gearchiveerde (oude) versie van de
  pagina, maar bezoeken de LIVE pagina op opgelicht.avrotros.nl met
  de URL die we via Wayback hebben gevonden, en lezen daar de ECHTE
  publicatiedatum uit de 'article:published_time' meta-tag.

DATUMFILTER (scope-bewaking)
  Tijdens het scrapen van elke detailpagina (stap 2) wordt de publi-
  catiedatum gecontroleerd. Artikelen buiten 2022-01-01 t/m 2025-12-31
  worden direct overgeslagen en NIET in de output-JSON of seen_urls.json
  gezet. Zo hoef je achteraf niet zelf te filteren, en blijft de
  dataset vanaf het begin scope-zuiver.

Chain of Custody (CoC) borging
  - Elke run krijgt een uniek run-ID en start-/eindtijd (met timezone).
  - Het outputbestand krijgt een SHA-256 hash, opgeslagen in een los
    .sha256 bestand zodat je kunt aantonen dat de data niet is gewijzigd.
  - Elke run wordt toegevoegd aan een doorlopend logbestand (run_log.csv).

Resumable scraping
  - 'seen_urls.json' onthoudt alle URLs die in eerdere runs al zijn
    bekeken (zowel gescraped als buiten-scope geweigerd), zodat je dit
    script gerust meerdere keren kunt draaien zonder dubbel werk.
