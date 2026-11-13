# Spotify AI DJ

Ein persönlicher Spotify-Controller für Windows: Genre-Modi mit lokalem Lernen,
ein „Für mich“-Mix aus deinem Spotify-Hörverhalten und spontane Musikwünsche per
Text oder Sprache. Das Dashboard läuft lokal unter **http://127.0.0.1:5001/**.

Der DJ steuert die Wiedergabe auf einem Spotify-Gerät. Er streamt selbst keine
Musik und integriert nicht den offiziellen Spotify AI DJ. Hosting und eine
öffentlich erreichbare Handy-Oberfläche sind noch nicht implementiert.

## Schnellstart für eine vorhandene Installation

- `start_dj_hidden.vbs` starten: Dashboard und DJ laufen ohne Konsolenfenster.
- Dashboard öffnen und einen Modus wählen. Beim Start wird noch keine Musik angefordert.
- Optional `scripts/dj_hotkey.ahk` mit AutoHotkey v2 starten.
- Für Sprache zusätzlich `start_voice_hidden.vbs` starten.
- Beenden über **Quit DJ**, **F13 + 9** oder den Terminal-Chat-Befehl `quit`.

Zum Neustarten warten, bis das Dashboard das Beenden meldet, dann den Hidden-Launcher
wieder ausführen. Der Voice-Client muss bei Bedarf ebenfalls neu gestartet werden.
Spotify selbst bleibt geöffnet und kann nach dem Beenden des DJs weiterspielen.

## Modi und Bedienung

| Taste / Dashboard | Funktion |
|---|---|
| F13 + 1 / Rap | American Rap aus dem lokalen Künstlerpool |
| F13 + 2 / DE Trap | German Trap aus dem lokalen Künstlerpool |
| F13 + 3 / K-Pop | K-Pop aus dem lokalen Künstlerpool |
| F13 + 4 / J-Pop | J-Pop aus dem lokalen Künstlerpool |
| F13 + 5 / Für mich | Persönlicher Mix aus Spotify-Hörverlauf und Top-Songs; ersetzt Global |
| F13 + 6 / Skip | Nächster DJ-Song |
| F13 + 7 / Ban Artist | Aktuellen entdeckten Künstler dauerhaft sperren; nicht für feste Pool-Künstler |
| F13 + 8 / Ban Track | Aktuellen Track dauerhaft sperren |
| F13 + 9 / Quit DJ | DJ, Dashboard-Server und laufenden Voice-Client beenden |
| F13 + V | Sprachaufnahme starten/stoppen; benötigt den separaten Voice-Client |

Das AHK-Skript enthält `RControl::F13`. Alternativ kann rechte Strg über die
Tastatursoftware auf F13 abgebildet werden. AHK verarbeitet die Ziffern-Hotkeys;
der Voice-Client verarbeitet F13 + V selbst.

Das Dashboard zeigt Künstler, Titel, Modus, Status und eine Fortschrittsleiste.
Die Leiste dient nur zur Anzeige, nicht zum Spulen. Logs trennen unter anderem
neue Songs, Moduswechsel und AI-Anfragen; interne Playback-Telemetrie wird nicht
als Konsolentext ausgegeben.

## Einrichtung unter Windows

Alle folgenden PowerShell-Befehle werden im Repository-Ordner ausgeführt.
Python muss die verwendete Syntax unterstützen (Python 3.10 oder neuer).
Die Startskripte erwarten ausdrücklich `.venv\Scripts\python.exe`.

```powershell
git clone https://github.com/NotMariusch/Spotify-AI-DJ.git
cd Spotify-AI-DJ
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Bei einer bestehenden Installation weder `.venv` noch Daten neu anlegen oder
löschen, nur um eine Codeänderung zu übernehmen.

### Konfiguration

Eine `.env` im Projektordner anlegen. Beispielwerte ersetzen; echte Schlüssel
und Tokens niemals committen oder in Logs veröffentlichen.

```dotenv
SPOTIFY_CLIENT_ID=deine_client_id
SPOTIFY_CLIENT_SECRET=dein_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
ANTHROPIC_API_KEY=dein_anthropic_api_key
LASTFM_API_KEY=dein_lastfm_api_key
DJ_VOICE_LANGUAGE=en-US
# Optionaler Start-Fallback, kein erzwungenes Wiedergabegerät:
# SPOTIFY_DEVICE_NAME=Mein PC
```

| Einstellung | Zweck |
|---|---|
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI` | Spotify-Anmeldung und Wiedergabesteuerung |
| `ANTHROPIC_API_KEY` | Für AI-Anfragen und dynamische KI-Abschnitte in „Für mich“; ohne Schlüssel funktioniert dort die lokale Auswahl aus Hördaten |
| `LASTFM_API_KEY` | Künstlerentdeckung in den lernenden Genre-Modi |
| `SPOTIFY_DEVICE_NAME` | Optionales bevorzugtes Gerät beim ersten Start ohne aktives Gerät |
| `DJ_VOICE_LANGUAGE` | Feste Sprache der Spracherkennung: standardmäßig `en-US`, alternativ etwa `de-DE` |
| `DJ_DEBUG=1` | Unterdrückt echte Playback-Startanfragen; für normalen Betrieb weglassen |

Die Redirect-URI muss zur Konfiguration der eigenen App im
[Spotify Developer Dashboard](https://developer.spotify.com/dashboard) passen.
Spotify-App-, Konto- und API-Berechtigungen müssen die verwendeten Endpunkte
zulassen. Zugriffsfehler werden nicht durch automatisches Neuanmelden umgangen.

Schlüssel für optionale Dienste gibt es bei
[Anthropic](https://console.anthropic.com/) und [Last.fm](https://www.last.fm/api/account/create).
AI-Aufrufe verursachen nutzungsabhängige Kosten; dieses Projekt garantiert weder
bestimmte Kosten noch eine bestimmte Anzahl Anfragen pro Guthaben.

### Spotify einmalig freigeben

Für die vollständige Einrichtung einschließlich „Für mich“ den DJ zunächst
beenden und dieses Skript ausführen:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\authorize_history.py
```

Im Browser anmelden und die Rechte bestätigen. Das Skript fordert
Wiedergabesteuerung sowie `user-read-recently-played` und `user-top-read` an.
Es ersetzt `.cache-dj` erst nach erfolgreicher Anmeldung; ein Abbruch lässt die
vorherige Anmeldung erhalten. Bei bestehender Einrichtung dient derselbe Befehl
zum Ergänzen der Hörverlaufsrechte. Danach den DJ neu starten.

Wer nur die bisherigen Modi einrichten möchte, kann die Basisanmeldung einmal
interaktiv prüfen, ohne Hörverlaufsrechte anzufordern:

```powershell
.\.venv\Scripts\python.exe -X utf8 -c "import sys; sys.path.insert(0, 'src'); import spotify_dj as dj; dj.connect().devices(); print('Spotify-Anmeldung geprüft.')"
```

Falls dabei eine Anmelde-URL oder Eingabeaufforderung erscheint, der angezeigten
Anleitung folgen. Ohne Hörverlaufsfreigabe zeigt „Für mich“ einen Hinweis; die
anderen Modi bleiben nutzbar. Der Dashboard-Server benötigt keine eigene
Spotify-Anmeldung. Vorhandene zusätzliche Rechte bleiben beim Token-Refresh erhalten.

### Start, Autostart und Beenden

```powershell
.\start_dj.bat                  # Mit sichtbarer Konsole
.\start_dj_hidden.vbs           # Versteckt
.\start_voice_hidden.vbs        # Optionaler separater Voice-Client
```

Für den Windows-Autostart können die vorhandenen Launcher und das AHK-Skript in
der Aufgabenplanung hinterlegt werden. Zugangsdaten vorher interaktiv einrichten.
Spotify darf beim Autostart noch geschlossen sein: Der DJ wartet auf eine Moduswahl
und bei angeforderter Wiedergabe gegebenenfalls auf ein verfügbares Gerät.

Server, DJ und Voice verwenden Betriebssystem-Sperren gegen doppelte Instanzen.
Der Schutz liegt im Python-Code, nicht im VBS-Launcher. Eine zurückgebliebene
Lock-Datei allein blockiert keinen Neustart; sie muss nicht gelöscht werden.

Der Server beobachtet Quit auch während langsamer DJ-Anfragen. Nach einer
begrenzten Wartezeit beendet er bei Bedarf nur seinen eigenen DJ-Prozessbaum.
AutoHotkey bleibt für spätere Starts verfügbar. Ein Neustart erfordert weder das
Beenden aller Python-Prozesse noch das Löschen von Cache- oder Lerndateien.

## „Für mich“: persönlicher Spotify-Mix

Der ehemalige Global-Modus verwendet jetzt deinen Spotify-Hörverlauf statt der
Vereinigung unserer festen Künstlerpools. Pro Aktualisierung liest er höchstens:

- 50 zuletzt gehörte Titel;
- je 50 Top-Songs aus ungefähr vier Wochen, sechs Monaten und einem Jahr.

Das sind vier begrenzte Leseanfragen, keine vollständige lebenslange Hörhistorie.
Kurzfristige Top-Songs werden stärker gewichtet, längerfristige Favoriten und
kürzlich gehörte Songs ergänzen sie. Der DJ bevorzugt weniger oft in dieser Session
gespielte Titel und wechselt Künstler, wenn geeignete Alternativen vorhanden sind.
Kleine Pools dürfen wiederholen. Sperren und Versionsfilter gelten weiterhin.

Die Auswahl bleibt im RAM. Beim erneuten Einstieg wird sie höchstens stündlich
aktualisiert; automatische Übergänge verwenden den vorhandenen Pool. Bei einer
fehlgeschlagenen Aktualisierung bleibt eine bereits geladene Auswahl erhalten.
Fehlen Rechte oder passende Songs, gibt es keinen stillen Rückfall auf den alten
Global-Künstlerpool.

Dieser Modus verändert keine dauerhaften Künstlerpools, Gewichte oder normalen
Recent-Dateien. Bekannte AI-Starts der laufenden DJ-Session
werden aus der Recent-Quelle ausgenommen. Ihr möglicher Einfluss auf Spotifys
aggregierte Top-Listen lässt sich nicht entfernen. Nach einem DJ-Neustart sind
diese temporären AI-Markierungen nicht mehr vorhanden.

### Dynamische Musikabschnitte

„Für mich“ startet sofort mit einer lokalen Auswahl. Mit eingerichtetem
`ANTHROPIC_API_KEY` plant Claude während der Wiedergabe jeweils einen nächsten
Abschnitt: Favoriten aus deinen Top-Listen, zuletzt gehörte Musik oder neue
Empfehlungen mit Bezug zu deinen Hördaten. Die KI wählt dafür eine musikalische
Richtung und eine kurze deutsche Einleitung. Diese erscheint beim bestätigten
Start des Abschnitts im Dashboard-Log; es gibt keine gesprochene Moderation.

Nach vier bestätigten Songstarts beginnt der nächste Abschnitt. Ein Skip zählt
als neuer Start; eine Pause oder ein fehlgeschlagener Start nicht. Künstler und
Songs rotieren innerhalb der Auswahl, soweit die Kandidatenmenge es erlaubt.
Entdeckungen müssen auf Spotify aufgelöst werden und durch die normalen Filter
passen. „Neu“ bedeutet hier außerhalb des geladenen Hördaten-Ausschnitts, nicht
nachweislich noch nie gehört. Musikalische Nähe ist eine KI-Einschätzung anhand
von Songwissen, keine Audioanalyse und keine Garantie für deinen Geschmack.

Pro Planung wird eine begrenzte Auswahl von maximal 48 Künstler-/Titel-Paaren
mit Quellenkategorie an Claude gesendet, dazu bis zu vier vorherige Abschnitte
und 24 zuletzt im persönlichen Mix gewählte Titel. Keine Spotify-IDs, Tokens
oder Abspielzeitpunkte werden als Modellkontext übertragen. Eine Planung
fordert höchstens sechs Songs von vier Künstlern an; nur Entdeckungen benötigen
zusätzliche Spotify-Suchen. Dabei entstehen laufende Claude-Nutzungskosten.

Es läuft höchstens eine Planung gleichzeitig. Bei Fehlern gelten begrenzte
Wiederholungen mit Wartezeit und Rate-Limit-Beachtung; der lokale Mix bleibt
verfügbar. Ohne KI-Ergebnis wechseln lokale Favoriten-/Verlaufsabschnitte weiter.
Ein anderer Modus oder eine neue AI-Anfrage verwirft ausstehende Planungen.
Die Auswahl bleibt temporär und verändert keine normalen Lerngewichte.

## AI-Modus: spontane Wünsche

Anfragen im Dashboard funktionieren auf Deutsch, Englisch und gemischt. Alternativ:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\dj_chat.py
```

Der Terminal-Chat akzeptiert auch `skip`, `ban`, `quit` und `1` bis `5`.
`exit` beendet nur das Chatfenster.

| Beispiel | Beabsichtigtes Verhalten |
|---|---|
| „Spiel Ado und Eve“ | Nur diese Künstler, freie geeignete Titelauswahl |
| „2000s pop from my childhood“ | Konkrete Hit-Vorschläge mit Zeitraum 2000–2009 |
| „Spiel diesen Song von diesem Künstler“ | Konkreter Song, keine ungefragten Ergänzungen |
| „Spiel diese drei Songs“ | Wunschliste in Reihenfolge; standardmäßig wiederholbar |
| „Spiel den Song und danach ähnliche Musik“ | Referenz zuerst, danach passende Vorschläge |
| „Mehr davon, aber weniger aggressiv“ | Folgeanfrage mit dem bisherigen Session-Bezug |
| „Heute keine Wiederholungen von vorhin“ | Bereits gehörte Session-Songs ausschließen |
| „Wiederholungen erlauben“ | Wiederholen erlauben, ohne Künstler-/Song-Ausschlüsse zu löschen |
| „Weniger von dem wie vorhin“ | Temporäre Verringerung, keine dauerhafte Abwertung |
| „Neue AI-Session“ | Temporären Kontext zurücksetzen und automatische Steuerung freigeben |

Der Standard ist kontinuierliche Rotation bis zu einer neuen Anfrage oder einem
DJ-Befehl. Eine konkrete Songliste ergänzt nicht automatisch ähnliche Songs.
„Nur einmal“ beziehungsweise ausdrückliche Wiederholungsverbote können die
Wiedergabe nach Ausschöpfen der Auswahl beenden. Ein einzelner angefragter Künstler
bleibt ein einzelner Künstler; die Rotation mischt keine fremden Künstler hinein.

Claude liefert strukturierte Wünsche, niemals ungeprüfte Spotify-IDs. Spotify
bestätigt Künstler und Titel. Mehrdeutige Künstlerprofile können anhand zweier
Songtitelbelege aufgelöst werden; der erste Suchtreffer oder Popularität allein
reichen nicht. Unauflösbare konkrete Wünsche werden nicht durch beliebige Songs ersetzt.

Jahresfilter beruhen auf verfügbaren Spotify-Veröffentlichungsdaten und gegebenenfalls
ISRC-Belegen für dieselbe Aufnahme. Fehlende Belege können passende Songs ausschließen.
Genre, Stimmung und musikalische Ähnlichkeit sind Modellurteile, keine von Spotify
bestätigten Eigenschaften oder Audioanalysen.

### Referenzsongs und Nachladen

„Songs zum lauten Mitsingen wie …“ wird in zwei Schritten behandelt:

1. Referenzen interpretieren und alle auf Spotify bestätigen. Serien-/Film-/Spielnamen
   werden als Kontext behandelt, nicht automatisch als Künstler.
2. Einen musikalischen Vergleich anfordern: Refrain, Rhythmus, Energie, Vortrag und
   Produktion. Nutzerkriterien haben Vorrang; beispielsweise wird eine weibliche
   Stimme nicht allein aus den Referenzen als Vorgabe abgeleitet.

Die erste Auswahl zielt auf etwa acht Ergänzungen, maximal zwölf. Kandidaten müssen
starke Modellbewertungen für die Nutzerkriterien und mindestens zwei musikalische
Referenzdimensionen erhalten. Unbekannter Klang oder fehlende nahe Treffer führen
zu einer Rückmeldung, nicht zu einer beliebigen Pop-Auswahl. Der Ablauf kann
musikalische Passung nicht garantieren.

Nach dem bestätigten Start lädt der DJ in einem Hintergrund-Thread weitere Portionen
nach, innerhalb des bestätigten Künstlerpools und mit denselben Auswahlkriterien.
Ziel sind bis zu 36 verschiedene Titel, nicht eine garantierte Mindestzahl.
Es gibt nur einen aktiven Nachladeauftrag und höchstens sechs Versuche pro Anfrage.
Zwischen erfolgreichen Versuchen liegen mindestens 30 Sekunden; bei Fehlern gelten
längere Wartezeiten und gegebenenfalls `Retry-After`.

Doppelte Titel und aktuelle Sperren werden vor der Übernahme geprüft. Ein Fehler
lässt den vorhandenen Pool unverändert. Neue Anfragen, Genre-/„Für mich“-Modus und
Quit entwerten alte Aufträge. Bereits laufende HTTP-Anfragen können noch auslaufen,
ihre Ergebnisse werden verworfen. Die Hintergrundaufrufe verursachen zusätzliche
AI-Nutzung. Reine erkannte Wiederholungsregel-Änderungen erzeugen keinen neuen AI-Pool.

### Temporärer Kontext und Datenweitergabe

AI-Sessions ändern keine dauerhaften Lerngewichte, Künstlerpools oder Discovery-Fortschritte.
Wiederholungszähler, Ausschlüsse und „weniger davon“ bleiben im RAM. Der Spotify-
Katalogcache ist davon getrennt und kann Metadaten auch für AI-Anfragen speichern.

Claude erhält die Anfrage und begrenzten Session-Kontext, darunter bis zu vier
vorherige Nutzeranfragen, die aktuelle Auswahl, gegebenenfalls Referenz-Titel,
letzten bestätigten Song und temporäre Regeln. Beim Nachladen werden bereits
gewählte Titel mitgegeben. Tokens und komplette Spotify-Kataloge werden nicht
als Modellkontext übermittelt. Für „Für mich“ gilt der oben beschriebene begrenzte
Hördaten-Kontext für die automatische Blockplanung.

## Spracheingabe

`start_voice_hidden.vbs` verwendet dieselbe `.venv`, schreibt Diagnosen nach
`data/voice.log` und läuft unabhängig vom AHK-Skript. Die benötigten Python-Pakete
stehen bereits in `requirements.txt`. Ein verfügbares Mikrofon und Mikrofonzugriff
für den Prozess sind erforderlich.

**F13 + V** startet die Aufnahme; ein zweiter Druck beendet sie und sendet die
Transkription als AI-Anfrage. Audio wird zur Google-Spracherkennung übertragen;
der erkannte Text wird anschließend wie ein Chatwunsch verarbeitet.

Standard ist **Englisch (`en-US`)**. Für Deutsch `DJ_VOICE_LANGUAGE=de-DE` in `.env`
setzen und den Voice-Client neu starten. Es gibt keine automatische Sprachumschaltung.
Künstler- und Songnamen können falsch erkannt werden; der erkannte Text erscheint
im Dashboard. Die Spracheinstellung betrifft nicht die mehrsprachige Text-AI.

## Playback, Geräte und Lernen

Ein aktives steuerbares Spotify-Gerät hat Vorrang vor `SPOTIFY_DEVICE_NAME`.
Das bevorzugte Gerät ist nur beim ersten Start ohne aktives Gerät ein Fallback.
Verschwindet ein Gerät später, wartet der DJ, statt heimlich zum PC zu wechseln.

Beim Übertragen desselben Songs im selben Album-Kontext auf Handy oder Alexa folgt
der DJ dem bestätigten Gerätewechsel. Ein anderer manuell gestarteter Song gibt
die automatische Steuerung frei, bis ein eindeutiger DJ-Befehl sie wieder aktiviert.
Eine normale Pause wird respektiert; der DJ startet nicht selbständig erneut.

Playback verwendet bewusst **Album-Kontext plus Track-Offset**. Der direkte
`uris=[…]`-Start war im Projekt reproduzierbar unzuverlässig. Eine erfolgreiche
Startanfrage allein gilt nicht als Wiedergabe: Der DJ prüft Track, Gerät,
Album-Kontext, Wiedergabestatus und Startposition in vier begrenzten Abfragen.
Fehlgeschlagene Starts erzeugen keine Hörbewertung.

Der DJ kann bei ausreichend gesicherter Hördauer etwa **1,5 Sekunden vor Schluss**
wechseln, damit Spotify nicht kurz den nächsten Albumtrack anspielt. Lückenhafte
Daten oder manuelle Eingriffe können diesen vorgezogenen Übergang verhindern.

Nur die vier Genre-Modi lernen dauerhaft:

| Ereignis | Wirkung |
|---|---|
| Mindestens 80 % tatsächlich beobachtete Hördauer, natürlicher Übergang | Künstlergewicht +0,15 |
| Expliziter DJ-Skip bei zuverlässigem Playback-Zustand | Künstlergewicht −0,10 |
| Moduswechsel, unklarer Zustand oder Sperrbefehl | Keine Abwertung dafür |
| AI oder „Für mich“ | Keine dauerhafte Gewichtsanpassung |

Die Skip-Regel hat aktuell **keine 25-Prozent-Schwelle**. Fortschritt durch Spulen
zählt nicht einfach als gehörte Dauer; eine Wiedergabe wird höchstens einmal
bewertet. Gewichte liegen zwischen 0,2 und 3,0 und bewegen sich beim Laden um
10 % der Distanz zu 1,0 pro verstrichener Woche zurück Richtung neutral.

Bei einem Genre-Künstlergewicht von 3,0 kann mit 25 % Wahrscheinlichkeit eine
Last.fm-Entdeckung vorgemerkt werden. Kandidaten werden auf Spotify geprüft;
mindestens fünf geeignete Tracks sind erforderlich. Fünf positiv bewertete
Probe-Wiedergaben führen zur dauerhaften Übernahme. Neue Genre-Entdeckungen sind
von spontanen AI-Wünschen und dem persönlichen Spotify-Mix getrennt.

## Fehlermeldungen und Grenzen

- **Spotify HTTP 429:** Eine gemeinsame Wartezeit schützt die Spotify-Aufrufe.
  Fehlendes, ungültiges oder nullwertiges `Retry-After` führt zu 30 Sekunden Ersatzwartezeit.
  Deren Ablauf garantiert nicht, dass Spotify wieder freigibt. SDK-Meldungen wie
  „Max Retries reached“ können zusätzlich vor unserer verständlichen Meldung erscheinen.
- **Claude-Timeout:** Interpretation hat 60 Sekunden Netzwerk-Zeitlimit, der
  musikalische Vergleich 90. Kein automatisches Wiederholen derselben Timeout-Anfrage;
  Hintergrund-Nachladen hat seine eigenen begrenzten Versuche mit Backoff.
- **Start nicht bestätigt:** Der Status nennt die zuletzt fehlende Bestätigung.
  Spotify prüfen und anschließend einen DJ-Befehl senden; keine Reauthentifizierung auf Verdacht.
- **AI wartet auf Antwort:** Wunsch konkretisieren. Ein unveränderter Prompt beseitigt
  keine fehlende Künstlerzuordnung oder tatsächlich mehrdeutige Aufnahme.
- **Für mich braucht Leserechte:** `scripts/authorize_history.py` interaktiv ausführen.
- **Playback-Daten fehlen:** Keine automatische negative Bewertung und kein erzwungener Neustart.

Die Oberfläche bindet an `127.0.0.1`; sie ist kein eingerichteter Remote-Zugang.
Das Projekt enthält auch Linux-Hilfsskripte unter `scripts/`. Diese Dokumentation
beschreibt den Windows-Betrieb; Linux-Hotkeys und Launcher benötigen eigene
Einrichtung und sind damit nicht als vollständiger Linux-Betrieb validiert.

## Dateien und Datenerhalt

| Datei / Bereich | Inhalt |
|---|---|
| `src/spotify_dj.py` | DJ-Steuerung, Playback, Filter, Genre-Auswahl, Integration der Modi |
| `src/playback_state.py` | Wiedergabezuordnung, Hördauer und Bewertungslogik |
| `src/ai_request.py`, `src/music_request.py`, `src/ai_session.py` | AI-Interpretation, Spotify-Auflösung, temporärer Kontext |
| `src/similarity_selection.py`, `src/background_pool.py` | Referenzvergleich und begrenztes Hintergrund-Nachladen |
| `src/personal_mix.py` | Persönliche Auswahl aus Spotify-Hördaten |
| `src/personal_director.py` | Temporäre Musikabschnitte und KI-Planung für „Für mich“ |
| `src/dj_server.py`, `src/dj_dashboard.html` | Lokaler Server und Oberfläche |
| `src/instance_lock.py`, `src/shutdown_signal.py`, `src/json_store.py` | Instanzschutz, Beenden und atomare JSON-Schreibvorgänge |
| `scripts/authorize_history.py` | Interaktive Spotify-Freigabe |
| `data/dj_memory.json` | Dauerhafte Genre-Lerngewichte |
| `data/track_cache.json` | Künstlerkatalogcache, regulär sieben Tage gültig; Nachladen bei Bedarf |
| `data/recent_tracks.json` | Persistente Recent-Historie der Genre-Modi |
| `data/discovered_artists.json` | Entdeckte Künstler und Probe-Fortschritt |
| `data/banned_tracks.json`, `data/banned_artists.json` | Dauerhafte Sperren |
| `data/dj.lock`, `data/server.lock`, `data/voice.lock` | Prozesssperren |
| `data/dj_input.txt`, `data/shutdown.signal` | Laufende Steuerung |
| `data/voice.log`, `data/dj_crash.log` | Voice-Diagnosen sowie DJ-Prozess-/Beendigungsdiagnosen |
| `.env`, `.cache-dj` | Zugangsdaten und Spotify-Token; niemals veröffentlichen |

Bestehende Lern-, Cache- und Authentifizierungsdaten **nicht zur Fehlerbehebung
pauschal löschen**. Vor Änderungen an solchen Dateien eine Sicherung erstellen.
Eine eventuell vorhandene alte `.cache` ist nicht der aktive DJ-Token und wird
nicht automatisch entfernt. Laufzeitdaten und Zugangsdaten sind in `.gitignore` erfasst.

## Entwicklung und Tests

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
node --test tests/test_progress_ui.cjs
git diff --check
```

Python-Tests mocken Spotify, Claude und Mikrofonzugriffe. Windows-Lifecycle-Tests
starten isolierte Testprozesse und lokale Testserver, keine echte Wiedergabe.
Node wird nur für die JavaScript-Tests benötigt. Automatisierte Tests ersetzen
nicht die Live-Prüfung von OAuth, Geräten, Mikrofon und musikalischer Qualität.

Nach Playback-Änderungen insbesondere Start, Skip, Pause/Fortsetzen, natürlichen
Übergang, manuellen Songwechsel und Gerätewechsel prüfen. Für Referenz-Anfragen
auch Hintergrund-Nachladen und einen Anfragewechsel während des Nachladens testen.
