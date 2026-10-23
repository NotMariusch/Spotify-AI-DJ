# Betriebsfixes – Phase 1

## Sicherungsstand vor den nächsten größeren Features

Stand: 23. Oktober 2026. Enthalten sind die Betriebsfixes unten, die bereinigte und in Abschnitte gegliederte Dashboard-Konsole, die Hotkey-Übersicht, die reine Fortschrittsanzeige sowie die Reparatur des Voice-Launchers und Voice-Fehlerbehandlung. Der ausdrücklich freigegebene Übergang ungefähr 1,5 Sekunden vor Songende ist ebenfalls umgesetzt.

Validierung dieses Stands: **71 Python-Tests und 7 JavaScript-Tests erfolgreich**, dazu Python-Syntaxprüfung und `git diff --check`. JavaScript-Tests: `node --test tests/test_progress_ui.cjs`. Der VBS-Trockenlauf benötigt Windows Script Host mit Zugriff auf seine Einstellungen.

Vom Nutzer live bestätigt wurden der ursprüngliche Playback-/Lernfix, Pause/Fortsetzen und der korrigierte Ado-Katalogabruf. Die zuletzt ergänzte Fortschrittsanzeige, der vorgezogene Übergang und der vollständige Voice-Weg wurden noch nicht zusammen live bestätigt. Das Standard-Mikrofonformat und der Voice-Launcher wurden ohne echte Aufnahme geprüft. Der Stand ist damit ein nachvollziehbarer Sicherungspunkt, keine Zusage vollständig abgeschlossener Live-Abnahme.

Secrets, OAuth-Caches, Lern-/Track-/Recent-Daten und Laufzeitlogs bleiben lokal und werden nicht versioniert. Das bevorzugte Spotify-Gerät wird lokal über `SPOTIFY_DEVICE_NAME` konfiguriert. Die größeren geplanten Arbeiten an Anfrageinterpretation, Songwünschen, Gesprächskontext, Rotation und Hosting sind noch nicht begonnen.

## Ergänzung: freigegebener Übergang vor Songende

Nach dem Live-Test wurde ein Wechsel etwa 1–2 Sekunden vor Schluss ausdrücklich freigegeben. Der DJ bereitet jetzt einmal pro Wiedergabe innerhalb der letzten 20 Sekunden einen passenden nächsten Titel aus bereits vorhandenen, gültigen Caches vor. Diese Vormerkung startet nichts, verändert keine Recent- oder Lerndaten und schreibt nichts in Spotifys Queue.

Bei etwa 1,5 Sekunden Restzeit genügt ein frischer, konsistenter Playback-Zustand des weiterhin eigenen Tracks auf demselben Gerät für den Übergang. Mindestens 80 Prozent tatsächlicher beobachteter Hörzeit und drei Sekunden stabiler Fortschritt sind nötig. Pause, ein Seek direkt ans Ende und unklare Daten lösen keinen vorgezogenen Wechsel aus. Die Wartezeit bis zur nächsten Prüfung wird an das bevorstehende Ende angepasst, statt dort pauschal fünf Sekunden zu schlafen. Der vorbereitete Titel startet weiter über den bewährten Albumkontext; eine erneute Gerätesuche entfällt nur für diesen unmittelbar zuvor bestätigten Gerätebezug. Künstlerentdeckung läuft erst nach einem bestätigten Start und verzögert dadurch nicht den ausgehenden Übergang.

Moduswechsel, neue Anfragen und manuelle Übernahme verwerfen die Vormerkung. AI-Vormerkungen bleiben im angefragten Pool und vom Lernen getrennt. Fehlgeschlagene Starts lösen keine wiederholten Ersatzstarts aus. Ohne passenden Cache-Kandidaten bleibt die konservative bisherige Albumwechsel-Erkennung erhalten. API-Ausfälle, Rate-Limits und hohe Spotify-Latenz können weiterhin einen rechtzeitigen Start verhindern; das Verhalten auf MOROR muss live geprüft werden.

Die Konsole trennt neue Songs, Moduswechsel, AI-Anfragen, Skip und Sperren durch Abschnittslinien. Das Dashboard stellt diese als dezente horizontale Linien mit Überschriften dar.

Zusätzliche Regressionstests: `tests/test_transitions.py`. Die folgende Phase-1-Beschreibung dokumentiert den ursprünglichen Stand vor diesem ausdrücklich freigegebenen Frühübergang.

Die Änderungen wurden am tatsächlichen lokalen Stand vorgenommen. Vorhanden waren Änderungen an `src/spotify_dj.py`, `scripts/dj_hotkey.ahk`, `start_dj.bat` und `start_dj_hidden.vbs`. Es wurden keine Repository-Anweisungen in `AGENTS.md` gefunden. Die Python-Umgebung ist `.venv`. Es wurde nichts gepusht oder committed, kein echter DJ gestartet und kein Spotify-/Claude-Aufruf für die Tests ausgeführt.

## Bestätigte Ursachen

- `track_finished()` überschrieb den Fortschritt des alten Tracks mit dem Fortschritt des neuen Tracks **vor** dem URI-Vergleich. Die anschließende Bewertung konnte dadurch einen vollständig gehörten Künstler abwerten.
- Jede andere URI galt als natürliches Ende. Auch Pausen nach 85 Prozent konnten neue Starts auslösen.
- Der Startrequest wurde ohne anschließende Playback-Prüfung als Erfolg gemeldet. Recent-Historie wurde schon vor dem Request gespeichert.
- Rekursive Fallbacks übernahmen `no_judge` und weitere Aufrufparameter nicht. AI-Aufrufe erhielten außerdem den globalen Pool als Fallback.
- Der aktuelle Code hatte bereits **keinen Explicit-only-Filter**. Der lokale Ado-Cache enthielt 116 nicht als explicit markierte Tracks aus genau fünf Live-Alben. Diese fallen korrekt unter die Live-/Tour-Filter. Beim Abruf wurden die Queryparameter falsch als `_get(params={...})` übergeben; laut installierter Spotipy-Implementierung entsteht dadurch ein verschachtelter Queryparameter. Album- und Track-Folgeseiten wurden nicht abgerufen. Die aktuelle [offizielle Album-Referenz](https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums) dokumentiert standardmäßig fünf und maximal zehn Alben pro Seite sowie `market` als Parameter. Der alte Cache passt zur Standardseitengröße; die genaue historische HTTP-Antwort wurde nicht aufgezeichnet. [Album-Tracks](https://developer.spotify.com/documentation/web-api/reference/get-an-albums-tracks) erlauben weiterhin bis zu 50 Einträge pro Seite.
- Ohne aktives Gerät wurde das erste gefundene Gerät benutzt. Gewöhnliche Playback-Fehler lösten `connect()` aus. Der Cache-Abruf fing Rate-Limits ab, weshalb der darüberliegende Prewarm-Abbruch nicht funktionierte. Eine konkrete Ursache des früher beobachteten Rate-Limits lässt sich rückwirkend nicht beweisen.
- PID-Dateien waren keine atomare Instanzsperre. Der Server startete seinen DJ vor der Prüfung, ob sein HTTP-Port verfügbar war.

## Verhalten nach der Änderung

- Der funktionierende Album-Kontext mit Track-Offset und `position_ms=0` bleibt erhalten. Es gibt keinen `uris`-Fallback. Unbrauchbare Album-/Track-Metadaten werden ausgeschlossen.
- Ein Start wird genau einmal angefordert und mit höchstens vier Prüfungen nach 1, 2, 2 und 3 Sekunden überprüft. Track, Gerät, Albumkontext und `is_playing` müssen passen. Erst dann werden Playback-Zustand und Recent-Historie übernommen. Nach unbestätigtem Start ist ein neuer DJ-Befehl nötig; es gibt keinen automatischen Startsturm.
- Playback-Evidenz ist an eine bestätigte Wiedergabe gebunden: Künstler, Modus, URI, Dauer, Gerät, AI-Schutz und Bewertung gehören zusammen. Eine Wiedergabe wird höchstens einmal bewertet.
- Hörzeit wird aus plausiblen Fortschrittsdifferenzen und monoton verstrichener Zeit ermittelt. Seek-Sprünge, Pausen und Beobachtungslücken werden nicht als gehörte Dauer angerechnet. Ab mindestens 80 Prozent beobachteter Hörzeit bleibt der Bonus `+0,15`. Ein eindeutiger DJ-Skip behält `-0,10`, bei unsicherer Datenlage entfällt die Abwertung. Moduswechsel und Track-Bans bleiben neutral.
- Die normale Pause löst auch kurz vor Trackende keinen Start aus. Fehlende Playback-Daten, API-Fehler und Gerätewechsel erzeugen keine automatische negative Bewertung.
- Nur ein plausibler Albumwechsel wird automatisch übernommen: letzte Probe höchstens 12 Sekunden alt, vorher laufend, höchstens 8 Sekunden Restzeit, zeitlich passender neuer Fortschritt, gleicher Albumkontext und gleiches Gerät sowie mindestens 80 Prozent beobachtete Hörzeit. Andere Wechsel geben die Steuerung frei. Die Moduswahl bzw. ein ausdrücklicher DJ-Befehl aktiviert sie erneut. Das Dashboard beobachtet externe Wiedergabe danach höchstens alle 15 Sekunden, ohne sie zu übernehmen.
- Jeder Künstler eines Fallback-Pools wird höchstens einmal versucht. Im AI-Modus besteht der Pool ausschließlich aus der aufgelösten Anfrage. Kein passender Track bedeutet einen erklärten Wartezustand, keinen globalen Ersatz. AI-Wiedergaben ändern keine Lerngewichte, Trial-Fortschritte oder dauerhaften Künstlerpools; ausstehende Discovery-Arbeit wird dabei nicht ausgeführt. Temporäre Künstlerzuordnung und Wiederholungsvermeidung bleiben zulässig; AI-Recent-Titel bleiben ausschließlich im Arbeitsspeicher und verändern nicht die persistente normale Recent-Historie. Explizite Ban-Befehle sind weiterhin bewusste Nutzeraktionen.
- Studio-Versionen ohne Explicit-Markierung bleiben zulässig. Die übrigen Versionsfilter bleiben bestehen. Die Explicit-Markierung ist nur ein Auswahlbonus. Wortgrenzen verhindern beispielsweise, dass das Live-Filterwort das Studio-Album „Believe“ ausschließt.
- Katalogabrufe verwenden flache Spotipy-Queryparameter mit `market=DE` und `limit=10` und berücksichtigen Album- und Track-Pagination mit festen Obergrenzen. Die installierte öffentliche Spotipy-Methode sendet noch `country` und ist für den aktuellen Vertrag deshalb hier ungeeignet. Erkennbar unerwünschte Alben werden schon vor dem Trackabruf übersprungen. Ein alter Cache ohne geeignete Tracks wird bei Bedarf neu geladen; fehlgeschlagene Teilabrufe ersetzen den bisherigen Cache nicht. Vorhandene Cache-Dateien wurden während der Entwicklung nicht verändert oder gelöscht.
- Alle Spotify-Aufrufe teilen einen Rate-Limit-Cooldown. `Retry-After` wird beachtet; fehlende, ungültige oder nullwertige Angaben führen zu 30 Sekunden Wartezeit. Es gibt keine zusätzlichen SDK-Retries und keine Reauthentifizierung bei gewöhnlichen Playback-Fehlern. Polling einer bestehenden Wiedergabe läuft nach dem Cooldown weiter; ein wegen Rate-Limit abgebrochener Start benötigt einen neuen Befehl.
- Kein vollständiges Prewarming beim Windows-Start: Kataloge werden bei Bedarf geladen. Fehlendes Spotify verhindert den Start des Dashboards nicht. Nach einer Moduswahl wartet ein ausstehender Start bei fehlendem Gerät und prüft alle 15 Sekunden erneut die Geräteverfügbarkeit, ohne Playback-Requests zu wiederholen.
- Bevorzugtes Gerät: `SPOTIFY_DEVICE_NAME` in `.env`, lokal auf `MOROR` gesetzt. Ohne diese Konfiguration wird ausschließlich ein aktives, nicht eingeschränktes Gerät verwendet. Mit Konfiguration muss das benannte Gerät vorhanden sein; ein anderes aktives Gerät wird nicht als Ersatz benutzt.
- DJ und Server halten je eine Betriebssystem-Dateisperre. Übrig gebliebene PID-Inhalte blockieren keinen Neustart. Die Dateien bleiben absichtlich liegen. Der VBS-Launcher überlässt die Sperrprüfung Python; sein versteckter Start über die vorhandene Batch-Datei bleibt erhalten. Der Server prüft Port 5001 vor dem DJ-Start und beendet seinen eigenen Kindprozess beim regulären Herunterfahren.
- Das Dashboard zeigt bestätigte Metadaten sowie Warte-, Pause-, Fehler- und Freigabezustände. Auch Moduswechsel über Hotkeys werden gemeldet.

## Grenzen

Spotify liefert keinen zuverlässigen Grund für einen Trackwechsel. Ein manueller Wechsel genau am erwarteten Ende innerhalb desselben Albumkontexts kann wie ein natürlicher Albumübergang aussehen. Die genannten Bedingungen begrenzen diese Mehrdeutigkeit, beseitigen sie aber nicht.

Stoppt Spotify am Ende ohne eindeutigen Album-Folgewechsel oder fehlen dort Messdaten, erfolgt konservativ kein automatischer Neustart. Ein DJ-Befehl setzt die Steuerung fort. Der tatsächliche Klang bzw. die lokale Audioausgabe lässt sich aus `is_playing` nicht beweisen. Beobachtete Hörzeit ist eine konservative Schätzung aus Spotify-Daten, keine Messung des Audioausgangs.

Neue vollständige Katalogabrufe können mehr Seiten benötigen als der alte fehlerhafte Abruf; sie werden nur bei Bedarf ausgeführt, überspringen ungeeignete Alben und unterliegen dem gemeinsamen Cooldown. Ein schon früher ausreichend gefüllter Cache wird regulär nach seiner TTL aktualisiert. Eine automatische Rate-Limit-Umgehung gibt es nicht.

Nicht live verifiziert: Spotify auf MOROR, echte Albumübergänge, Spotify-API-Antworten beim korrigierten Katalogabruf, Claude, Razer/AHK und Aufgabenplanung nach Windows-Neustart. Windows-Dateisperren wurden hingegen mit einem echten zweiten Python-Prozess getestet. Aufgabenplanung, Hotkeys und laufende Nutzerprozesse wurden nicht umgestellt oder beendet.

## Tests

Aus dem Repository-Verzeichnis:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m py_compile src/spotify_dj.py src/ai_request.py src/dj_server.py src/playback_state.py src/instance_lock.py
git diff --check
```

Ergebnis: **39 Tests erfolgreich**, Syntaxprüfung und `git diff --check` erfolgreich. Die Tests verwenden gemockte Spotify-Antworten und gemockte Persistenzfunktionen. Der Lock-Test verwendet ausschließlich ein eigenes temporäres Verzeichnis im Repository. Der Dashboard-Test verwendet den Flask-Testclient ohne HTTP-Listener und ohne DJ-Kindprozess.

## Kurze Windows-Live-Checkliste

1. Für den ersten Wechsel auf die neue Sperrlogik den bisherigen DJ und Dashboard-Server kontrolliert beenden. Anschließend `start_dj.bat` starten und `http://127.0.0.1:5001` öffnen. AHK kann weiterlaufen. Ein zweiter Launcher-Aufruf darf keinen weiteren DJ starten.
2. MOROR zunächst nicht verfügbar lassen und einen Modus wählen: Wartezustand, keine Wiedergabe auf iPhone/Echo. Spotify auf MOROR öffnen; nach Geräteerkennung soll genau ein bestätigter Start erfolgen. „Start angefordert“ darf noch keine bestätigte Wiedergabe vortäuschen.
3. Einen Song ohne Seek vollständig hören: beim plausiblen Albumübergang einmal `+0,15`, niemals `-0,10`; danach neuer DJ-Track. Einen weiteren Song mit F13+6 explizit skippen: einmal `-0,10` im normalen Modus.
4. Mitten im Song direkt in Spotify einen anderen Song starten: Status „DJ freigegeben“, keine sofortige Übernahme und keine Abwertung. Mit Moduswahl/F13+6 wieder übernehmen. Pause/Fortsetzen auch nahe dem Ende prüfen; Seek ans Ende darf keine künstliche Hörzeit erzeugen.
5. „Play Ado and Eve“ senden: der alte unvollständige Ado-Cache soll bei Bedarf erneuert werden. Nur Ado/Eve sind zulässig. Keine Gewichts-/Trial-/Pooländerung bei AI-Skip, AI-Fallback oder anschließendem Moduswechsel. Der Katalogcache darf sich normal aktualisieren; AI-Recent-Titel bleiben nur in der laufenden Session, die persistente normale Recent-Historie bleibt unverändert.
6. Den geplanten Windows-Autostart beim nächsten regulären Neustart prüfen: Dashboard erreichbar, AHK unverändert, keine doppelten Instanzen. Ein echtes Rate-Limit nicht absichtlich provozieren; dessen Verhalten ist gemockt abgedeckt.

## Vorgesehene nächste Schritte – noch nicht implementiert

Nach Rückmeldung zuerst strukturierte Anfrageinterpretation und verlässliche Auflösung konkreter Songs und Songlisten. Danach temporärer Gesprächs-/Session-Kontext, anschließend Rotation und Wiederholungsvermeidung. Hosting und mobile Nutzung folgen später nach Prüfung der dann aktuellen offiziellen Spotify-Anforderungen. Es wurde keine Phase-2-Implementierung begonnen.
