# Dashboard und PostgreSQL-Analytics

Diese Einrichtung nutzt die **vorhandene** PostgreSQL-Datenbank, legt keinen
zweiten Server an und verändert den Trading-Bot nicht. Das separate Dashboard
zeigt maximal je 100 Snapshots und Ereignisse; es hat keine Order-Endpunkte.
Basic Auth und eine SELECT-only-DB-Rolle beschränken den lesenden Zugriff.
Die Anzeige ist kein Börsenstatus und kein Nachweis, dass der Bot läuft.
Diese lokale Variante verzichtet ausdrücklich auf PostgreSQL-TLS; sie ist
deshalb **nicht abhör- oder manipulationssicher**, auch nicht im lokalen Netz.

## Temporärer Betrieb auf dem CM5-Cluster

Auf `rasp-clu-01` wurde zusätzlich zur unten beschriebenen allgemeinen
Einrichtung ein temporärer Dashboard-Test im Namespace `trading` eingerichtet:
Deployment `trading-dashboard-local-52247d45`, Datenbank `appdb`, Schema
`trading_analytics`, vorhandene Rollen `tradingbot_writer` und
`trade_dashboard_reader`. Der Writer benötigt Datenbank-CREATE; der Leser
hat SELECT, aber kein INSERT auf den Analytics-Tabellen. Weitere öffentliche
Datenbankrechte sind damit nicht generell ausgeschlossen.

Der Dashboard-Pod verwendet Host-Netzwerk, da die Verbindung über den
Cluster-Service vom vorhandenen PostgreSQL-`pg_hba.conf` abgelehnt wurde. Er
bindet nur an `127.0.0.1:8080` auf Node 1. Lokale Prozesse auf Node 1 können
den Port ebenfalls erreichen; Basic Auth und SSH-Zugang entsprechend schützen.
Zugangsdaten liegen auf Node 1 ausschließlich in der Datei
`/mnt/nvme_data/trading_simulation_59325f0/dashboard-access.txt` (Modus 0600)
und im Kubernetes-Secret. Für den Zugriff vom eigenen Rechner:

```sh
rtk proxy ssh -N -o BatchMode=yes -o StrictHostKeyChecking=yes -L 127.0.0.1:8080:127.0.0.1:8080 raspi-cm5-node-1.kienzle
```

Danach `http://127.0.0.1:8080/` im lokalen Browser öffnen. Die Zugangsdaten
bei Bedarf selbst über SSH aus der geschützten Datei auf Node 1 lesen;
Passwörter nicht in Chat, Logs oder Repository übernehmen. Den SSH-Prozess zum
Beenden des Zugriffs mit Ctrl+C schließen.

Der bestehende Dry-Run wurde einmal mit unverändertem Portfolio auf den
Analytics-Writer umgestellt. Die alte Instanz ist gestoppt und darf nicht
parallel gestartet werden. Ein Portfolio- und Journal-Abzug unmittelbar vor
dem Neustart liegt auf Node 1 unter
`/mnt/nvme_data/trading_simulation_59325f0/pre-analytics-52247d45/`.
Das verwendete Dashboard-Manifest und die Image-Bausteine liegen auf Node 1
unter `/mnt/nvme_data/trading_simulation_59325f0/deployment-52247d45/`.
Analytics beginnt erst mit der Umstellung, ist Best Effort und keine
Recovery. Der Dry-Run hat weiterhin keinen automatischen Neustart; Images und
Laufzeitmanifeste sind nicht durch einen versionierten Release reproduzierbar.
Dies ist keine Freigabe für Live-Orders.

## Vorbedingungen und Sicherheitsgrenzen

* Das vorhandene PostgreSQL-Overlay initialisiert `appdb`/`trading_analytics`
  nur bei **neuem** Datenverzeichnis. Bei bestehenden Daten zuerst das Schema
  und beide Tabellen samt Indizes prüfen; `scripts/init_postgres_analytics.sql`
  ist das vorhandene Schema. Schemaänderungen, Backup und Rollback separat
  planen. Das SQL unten ändert Eigentümer und entzieht PUBLIC Rechte an diesen
  Objekten; andere Nutzer können dadurch Zugriff verlieren.
* Das bisherige StatefulSet konfiguriert **kein TLS**, der bisherige
  ConfigMap-Standard ist `sslmode=disable` und `trading_db` ist der initiale
  PostgreSQL-Superuser. Statt des Superusers getrennte Rollen verwenden.
  Writer-, Dashboard- und administrative DB-Verbindungen mit
  `sslmode=disable` übertragen Abfragen und Daten unverschlüsselt; der Server
  wird nicht kryptografisch authentifiziert. Teilnehmer auf dem Netzwerkpfad
  können mitlesen und Verbindungen manipulieren oder sich als Server ausgeben.
  PostgreSQL-SCRAM statt Passwort-Klartextverfahren verwenden; SCRAM ersetzt
  weder Transportverschlüsselung noch Serverauthentifizierung.
* Das lokale Netz allein ist keine Sicherheitsgrenze. DB-Service und Port 5432
  nicht extern veröffentlichen; Netzwerkzugriff mit Firewall, PostgreSQL
  `pg_hba.conf` und gegebenenfalls durch die tatsächlich durchgesetzten
  Kubernetes-NetworkPolicies auf erforderliche Clients beschränken. Betreiber
  muss die Wirksamkeit prüfen; dieses Overlay erstellt keine NetworkPolicy.
  Wenn andere Systeme das Netz oder Nodes nicht vertrauenswürdig mitbenutzen,
  diese Klartextvariante nicht verwenden.
* Kubernetes-Secrets sind nicht automatisch verschlüsselt: Encryption at rest,
  enges RBAC für Secrets, Pods und `pods/portforward`, vertrauenswürdige Nodes,
  getrennte Zugangsdaten, Rotation und Audit sind Betreiberpflicht. Keine
  Secrets, DSNs oder echten Passwörter ins Repository, Shell-History, Logs,
  Image oder Kommandozeilenargumente schreiben. Secret-Bereitstellung
  über den bestehenden Secret-Manager bzw. gesicherte Dateien organisieren.
  Nach Rotation Pods neu starten; Umgebungsvariablen aktualisieren sich nicht.

## Rollen einmalig einrichten

Als administrativer Benutzer mit Rollen- und Objektverwaltungsrechten
**zur richtigen bestehenden Datenbank** ausführen (das DSN liegt beispielsweise
in `PGSERVICE`, dort für diese Variante `sslmode=disable` setzen; Zugangsdaten in einer
geschützten libpq-Passdatei). Der Vorgang scheitert absichtlich, wenn die beiden
Rollennamen bereits existieren; nicht blind erneut ausführen. Die Verbindung
bleibt auch beim Setzen der Passwörter unverschlüsselt und anfällig für aktive
Angriffe; nur auf einem entsprechend kontrollierten Netzwerkpfad verwenden.

```sh
rtk proxy psql -X -v ON_ERROR_STOP=1 --single-transaction -f k8s/overlays/trading-dashboard/analytics-roles.sql
rtk proxy psql -X
```

In der administrativen interaktiven psql-Sitzung anschließend jeweils
`\password trading_analytics_writer` und `\password trading_dashboard_reader`
verwenden. Vorher sind beide Login-Rollen ohne Passwort nicht für
Passwortauthentifizierung nutzbar. Separat aus dem Secret-Manager die
Passwörter bereitstellen, nicht identische Passwörter verwenden.

Die Tabelleigentümerrolle Writer erhält absichtlich mehr als INSERT: Der
bestehende Bot führt beim Verbinden `CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS`
aus. Deshalb benötigt er Datenbank-CREATE, Schema-CREATE und Eigentümerschaft
an den Tabellen; er kann Tabellen und Daten ändern oder löschen und andere
Schemas erzeugen. Das ist **keine append-only-Sicherheitsgrenze**. Die
Leserrolle besitzt nur USAGE und SELECT auf diesen zwei Tabellen und
`default_transaction_read_only` als zusätzliche Vorsichtsmaßnahme, nicht als
Berechtigungsersatz. SQL entzieht PUBLIC-Rechte nur auf den genannten Objekten;
es ändert keine anderen Rollen oder Datenbanken. Vor Freigabe zusätzlich
andere PUBLIC- und explizite Grants, Rollenmitgliedschaften, Funktionen,
Views und TEMP-Rechte in der gesamten Datenbank prüfen und erforderlichenfalls
entziehen. Keine privilegierten Mitgliedschaften oder zusätzliche Grants an
den Leser vergeben. Schema-/Tabellenbesitzer und Superuser bleiben mächtig.
Neue Tabellen und später geänderte Grants erfordern erneute Prüfung.

## Writer: vorhandenen Dry-Run ausdrücklich sicher konfigurieren

Das vorhandene `k8s/overlays/analytics-writer` startet bereits den Bot; **kein
zweites Exemplar daneben starten**. Es ist nicht Teil dieses
Dashboard-Overlays: Es verwendet `hostPath`, optional breite Secrets,
`trading_db` und die unverschlüsselte ConfigMap. Vor Inbetriebnahme dessen
Deployment und Konfiguration gezielt durch den Betreiber anpassen:

1. `DRY_RUN=true`, `ANALYTICS_DB_ENABLED=true`,
   `ANALYTICS_DB_SCHEMA=trading_analytics` und
   `ANALYTICS_DB_URL` als Secret mit einer libpq-DSN für
   `trading_analytics_writer`, `appdb`, den internen DB-Host und
   `sslmode=disable` setzen. Eine gesetzte URL ersetzt die einzelnen
   DB-Einstellungen; insbesondere erzwingt `ANALYTICS_DB_SSLMODE` bei
   gesetzter URL **kein** TLS.
2. Der Bot lädt die `.env` mit `override=True`: Eine Datei auf dem bestehenden
   Host-Mount kann `DRY_RUN`, die DSN und andere Umgebungswerte überschreiben.
   Sie muss vor Start entfernt bzw. durch eine kontrollierte leere Datei
   ersetzt werden. Auch alternative `POSTGRES_*`-Werte, Runtime-Secrets,
   Deployment-Rollouts und Dateirechte auditieren. Effektive Konfiguration
   ohne Ausgabe von Secrets kontrollieren. Keine Exchange-Schlüssel mit
   Handelsrechten im Dry-Run bereitstellen.
3. Ein Replikat ist keine verteilte Sperre. Keine parallelen Writer starten;
   Rolling Updates, manuelle Prozesse und alte Pods ebenfalls ausschließen.
   Testweise Snapshot-Eingang und `dry_run=true` in PostgreSQL prüfen. Ein
   gemeldeter Snapshot beweist weder korrekte Isolation noch lückenlose Daten.

Dieses Repository liefert absichtlich keinen vermeintlich sicheren Writer-Patch:
Host-Datei, bestehende Secrets und Netzwerkzugriff sind ohne Kenntnis der
Installation nicht sicher überschreibbar. Bis diese Voraussetzungen erfüllt
sind und das Klartextrisiko bewusst akzeptiert ist, nicht deployen.

## Dashboard deployen

1. Image aus dem Repository-Root bauen und in die interne Registry übertragen;
   Deployment vor Anwendung auf das geprüfte, unveränderliche Image-Digest
   ändern. `latest` ist nur ein lokaler Platzhalter.

   ```sh
   rtk proxy docker build -f docker/trading-dashboard.Dockerfile -t trading-dashboard:latest .
   ```

2. Im Namespace `trading-bot` extern ein Secret
   `trading-dashboard-credentials` mit `DASHBOARD_USER`, `DASHBOARD_PASSWORD`
   und `DASHBOARD_DATABASE_URL` bereitstellen. Die DSN nutzt ausschließlich
   `trading_dashboard_reader`, den internen DB-Host und ausdrücklich
   `sslmode=disable`; URL-Passwort gegebenenfalls korrekt percent-encodieren.
   Für Basic Auth starke separate Zugangsdaten verwenden. Das Manifest setzt
   `DASHBOARD_DB_SCHEMA=trading_analytics` fest und benötigt kein CA-Volume.
3. Nach Bereitstellung des Secrets deployen und ausschließlich lokal zugreifen:

   ```sh
   rtk proxy kubectl apply -k k8s/overlays/trading-dashboard
   rtk proxy kubectl -n trading-bot rollout status deployment/trading-dashboard
   rtk proxy kubectl -n trading-bot port-forward --address 127.0.0.1 deployment/trading-dashboard 8080:8080
   ```

   Auf demselben Rechner `http://127.0.0.1:8080/` öffnen und Basic-Auth-
   Zugangsdaten eingeben. HTTP/Basic Auth ist Klartext: Zugangsdaten niemals
   über das lokale Netz senden, Port-Forward nicht an `0.0.0.0` binden und
   keinen Service, Ingress oder ungeschützten Proxy dafür bereitstellen.
   Die Anwendung bindet nur an Pod-Loopback; eine normale Service-Weiterleitung
   zur Pod-IP funktioniert nicht. Kubernetes-API-Zugriff und Berechtigung
   `pods/portforward` per RBAC auf befugte Operatoren begrenzen; auch
   `pods/exec`, Pod-Erstellung und Secret-Lesen können Zugriff ermöglichen.
   Den verwendeten Rechner und dessen lokale Benutzer schützen. Port-Forward
   schützt nicht die separate Klartextverbindung vom Dashboard zu PostgreSQL.

4. Verifizieren: Ohne/mit falscher Authentifizierung 401, mit richtiger
   Authentifizierung Dashboard; Port-Forward nur auf localhost erreichbar,
   weder Service noch Ingress für das Dashboard vorhanden. Mit dem Leser ein SELECT aus beiden Tabellen
   testen sowie INSERT/UPDATE/DELETE/TRUNCATE außerhalb einer absichtlich
   read-only Transaktion: alle müssen an DB-Rechten scheitern. Keine
   Zugangsdaten in Diagnoseausgaben ablegen. 503 bedeutet fehlende Auth-
   Konfiguration oder DB-Fehler, kein gesundes Dashboard.

## Option: später PostgreSQL-TLS einführen

Der Betreiber muss PostgreSQL-TLS einschalten, ein Serverzertifikat mit dem
verwendeten DNS-Namen im SAN installieren und die CA unabhängig prüfen.
`pg_hba.conf` auf `hostssl` mit SCRAM beschränken und keinen passenden
Klartext-`host`-Fallback zulassen. Beispielsweise muss bei
`postgres-analytics.trading-bot.svc` genau dieser Name im Zertifikat stehen.
Die CA-Datei als eigenes read-only ConfigMap-Volume sowohl beim Dashboard als
auch beim Writer mounten und `PGSSLROOTCERT` auf den jeweiligen Dateipfad
setzen; nur öffentliche CA-Zertifikate, keine privaten Schlüssel, verwenden.
Beide Secret-DSNs auf `sslmode=verify-full` und den zertifizierten Hostnamen
umstellen; die administrative Verbindung ebenfalls umstellen. Secrets und
Deployment aktualisieren und Pods neu starten. Eine CA-ConfigMap ist in der
aktuellen Klartextkonfiguration nicht erforderlich. Anschließend echte TLS-
Verbindung sowie Ablehnung bei falscher CA, falschem Hostnamen und
Klartextverbindung testen; bloßes `sslmode=require` überprüft den Servernamen
nicht ausreichend. Diese TLS-Option ist hier nicht vorkonfiguriert und ersetzt
nicht RBAC und localhost-Port-Forward für das Dashboard.

## Grenzen und Tests

Analytics ist best effort, kein dauerhaftes Transaktionsjournal und keine
PostgreSQL-Portfolio-Recovery: Writes, lokaler Zustand und Risikostatus sind
nicht atomar, Lücken oder Duplikate sind möglich, Snapshots können veraltet
sein. Auch die DB-Fehlerbehandlung fängt nicht alle psycopg-Fehler ab; ein
ununterbrochener Bot-Betrieb bei DB-Ausfall ist nicht garantiert. Die Anzeige
mischt keine Währungen/Modi in einem Chart, zeigt aber historische LIVE- und
DRY-RUN-Daten; der zuletzt angezeigte Modus ist kein laufzeitverifizierter
Schalter. `DRY_RUN_REQUIRE_POSTGRES_RECOVERY=true` verweigert aktuell den
Start, statt Recovery zu implementieren. Weitere Grenzen stehen in
`docs/postgres_analytics_schema.md`.

Lokale Tests ohne Cluster:

```sh
rtk proxy .venv/bin/python -m pytest -q tests/test_trading_dashboard.py tests/test_trading_dashboard_deployment.py
```

Optionale echte PostgreSQL-Tests benötigen eine **isolierte Wegwerf-Datenbank**
und einen Superuser über `TRADING_DASHBOARD_SETUP_TEST_DSN`; der Setup-Test
erstellt zufällig benannte Rollen/Objekte und rollt die Transaktion zurück.
Die bestehenden Dashboard-Integrationstests benötigen zusätzlich
`TRADING_SAFETY_TEST_DSN`. Diese Tests ersetzen weder Server-TLS- noch
RBAC-Prüfungen im Zielsystem. Ohne diese DSNs werden sie übersprungen.
