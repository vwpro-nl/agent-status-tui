# Projectstatus — agent-status-tui

Laatst bijgewerkt: 2026-09-20 (05:35 lokaal).

## Doel

`agent-status-tui` is het centrale lokale terminaldashboard voor de geïnstalleerde
coding agents. Het bevat twee onderdelen die verschillende vragen beantwoorden:

- `./agent-status-tui` — het bestaande AGENT STATUS-dashboard: hoe staan de
  agents er **nu** voor (5h/week-quota, reset, freshness).
- `./agent-status-tui keepalive` (+ `history`/`monitor`) — de standalone
  keepalive: houdt de drie agent-sessies actief met een activity-aware,
  klokgebonden schema, en legt van iedere cyclusslot chronologisch vast of
  er gepingd of geskipt is, en wat de provider daarna native rapporteert
  voor 5h/week-quota en reset.

De calibrator (probe-kalibratie/boundary-onderzoek) blijft verwijderd/
gearchiveerd; zie "Calibrator (gearchiveerd)" hieronder. Dit document is nu
op zijn tweede substantiële revisie sinds die verwijdering: de eerste
introduceerde de keepalive-history/observe/render-laag bovenop het bestaande
3001s-interval-model; deze revisie vervangt dat interval-model zelf door een
klokgebonden, activity-aware schema (zie hieronder).

## Authority

Gebruik bij een nieuwe werksessie deze volgorde:

1. `AGENTS.md` — permanente repositoryregels en ontwikkelafspraken.
2. `STATUS.md` — actuele projectstatus en overdracht tussen werksessies.
3. `COMMANDS.md` — praktische commando's.
4. `README.md` — productgedrag, architectuur en gebruik.
5. huidige code, tests en git-history — technische authority wanneer
   documentatie achterloopt.

## Huidige repository-status

Working tree bevat (nog niet gecommit — commit/push is voor deze taken niet
geautoriseerd):

- verwijderd: `agentstatus/calibrator/` (volledig) en
  `tests/test_calibrator*.py` (5 bestanden), plus de `calibrator`-route uit
  `agentstatus/cli.py` en de bijbehorende calibrator-tests in `tests/test_cli.py`.
- nieuw (sinds de eerste keepalive-history-taak): `agentstatus/keepalive/history.py`,
  `agentstatus/keepalive/observe.py`, `agentstatus/keepalive/render.py`,
  `tests/test_keepalive_history.py`, `docs/calibrator-research.md`.
- volledig herschreven (deze taak, klokgebonden scheduler): `agentstatus/keepalive/core.py`
  (nieuw scheduling-model, zie hieronder), `agentstatus/keepalive/cli.py`
  (CycleRunner-integratie i.p.v. drie onafhankelijke per-agent threads),
  `agentstatus/keepalive/render.py` (ACTION/LAST ACTIVITY-kolommen,
  `--agent`-filtering laat de AGENT-kolom weg), `tests/test_keepalive.py`
  (volledig herschreven rond het nieuwe model).
- gewijzigd: `agentstatus/doctor.py` (interval-check vervangen door
  cycle/stagger-check; eerdere keepalive-history-checks blijven ongewijzigd),
  `tests/test_doctor.py`, `README.md`, `COMMANDS.md`, `AGENTS.md`.

Oorspronkelijke HEAD vóór al deze taken: `e3a2a49` ("Add standalone agent
keepalive and doctor"), branch `main`.

Git-tag `calibrator-research-archive` (annotated, op `e3a2a49`) bevat de
volledige calibrator-implementatie zoals die bestond vóór verwijdering. Zie
`docs/calibrator-research.md` voor wat erin zit, waarom de calibrator is
losgelaten, en hoe de oude code exact via Git is terug te vinden.

## Dashboard

Ongewijzigd door beide keepalive-taken. Zie `README.md` voor het volledige
gebruikers- en renderingcontract. Regressietests
(`tests/test_render.py`, `test_poll.py`, `test_adapters_*.py`, `test_model.py`,
`test_cli.py`) draaien groen; live `./agent-status-tui --once` is op dit
systeem herhaaldelijk bevestigd correct.

## Keepalive — klokgebonden, activity-aware schema (huidig model)

Het eerdere model (vast interval van 3001s sinds de eigen laatste
ping/activiteit per agent, drie onafhankelijke achtergrondthreads) is
**vervangen**. Het huidige, definitieve operationele model:

### Scheduling

- Vaste cycli op iedere hele en halve klokminuut: `HH:00` en `HH:30`
  (`CYCLE_SECONDS = 1800` in `agentstatus/keepalive/core.py`).
- Iedere cyclusgrens wordt **vers** herberekend uit de werkelijke klok
  (`next_boundary(now)`), nooit additief vanaf een vorige grens — een trage
  cyclus laat de volgende grens dus nooit driften.
- Na (her)start wacht de daemon simpelweg op de eerstvolgende `:00`/`:30`.
  Geen catch-up/backfill van gemiste sloten, geen "meteen pingen bij
  restart"-uitzondering: elk slot is een op zichzelf staande, klokgebonden
  beslissing, dus wachten kost nooit meer dan 30 minuten en is nooit
  schadelijk.
- Vaste agentvolgorde: `claude`, `codex`, `grok` (ongewijzigd t.o.v. de
  bestaande `AGENTS`-tuple). Iedere cyclus bezoekt ze in exact deze
  volgorde, met een vaste stagger van 5 seconden per agent
  (`AGENT_STAGGER_SECONDS = 5`): claude op `HH:00:00`/`HH:30:00`, codex 5s
  later, grok 10s later. Sequentieel, single-threaded binnen de cyclus (geen
  gelijktijdige providerprocessen), maar één agentfout (ping-fail of een
  onverwachte exceptie) blokkeert nooit de volgende agent in dezelfde
  cyclus, en beïnvloedt nooit de eerstvolgende cyclusgrens (die wordt sowieso
  vers herberekend).

### PING of SKIP

Per agent, per slot: `KeepaliveAgent.decide(now, last_activity)`.

- Onbekende activiteit → altijd `PING`.
- Activiteit binnen `ACTIVITY_WINDOW_SECONDS = 1800` (exact één cyclus, dus
  30 minuten) vóór `now` → `SKIP` (geen ping, geen modelturn, geen
  state-write).
- Activiteit ouder dan dat venster (of ogenschijnlijk in de toekomst — een
  klokscheve/rare bron wordt nooit als "recent actief" gelezen) → `PING`.

Dit venster is bewust vast en simpel — geen calibratorachtige adaptieve
logica. Activiteit van de ene agent kan de beslissing van een andere agent
nooit beïnvloeden: elke `KeepaliveAgent` leest uitsluitend de eigen
provider's `detect_activity()`.

`--once` blijft een geforceerde, directe live/diagnostische ping (bypasst de
SKIP-beslissing volledig, zoals voorheen) — dit is bewust ongewijzigd
gebleven t.o.v. het vorige contract.

### Ieder slot wordt opgeslagen

Elke cyclus levert per agent één history-event op, ongeacht `PING` of
`SKIP`. Per event (uitgebreid t.o.v. de vorige revisie):

- `timestamp`, `agent`, `action` (`"ping"`/`"skip"`);
- `last_activity` — de activiteitstijd waarop de beslissing is gebaseerd, of
  `null`;
- `ping_status` (`"ok"`/`"fail"`/`null` bij SKIP), `ping_error`;
- `five_hour`/`weekly` — provider-native quota/reset, ongeacht PING/SKIP
  (zie hieronder), of `null`;
- `observation_status`/`observation_detail` — vaste, niet-geheime tekst.

### Providerobservatie blijft ongewijzigd van aanpak

`agentstatus/keepalive/observe.py` is niet aangepast door deze taak: nog
steeds zelfstandig (geen import van `agentstatus.env`/`adapters`), en wordt
nu bij **ieder** slot aangeroepen (ook bij SKIP) — nooit een extra
modelturn. Grok's `creditUsagePercent: null`-bevinding (zie hieronder) blijft
staan; er is geen aanwijzing gevonden die Grok-gedrag rechtvaardigt aan te
passen.

### Weergave

Gezamenlijke geschiedenis:

    TIME  AGENT  ACTION  LAST ACTIVITY  PING  5H  RESET  WEEK  RESET

Per-agent (`--agent ...`) laat de AGENT-kolom volledig weg:

    TIME  ACTION  LAST ACTIVITY  PING  5H  RESET  WEEK  RESET

`PING` toont `OK`/`FAILED`/`--` (`--` bij een SKIP — er is dan geen ping
geweest om te beoordelen). Bij een `FAILED`-ping staat de veilige foutreden
op een aparte detailregel eronder, zoals voorheen. Een `SKIP` toont de reële
`LAST ACTIVITY` (of `--` indien onbekend) en desondanks de
providerobservatie waar beschikbaar.

### CLI

```
./agent-status-tui keepalive history [--agent claude|codex|grok] [--state-dir ...]
./agent-status-tui keepalive monitor [--agent ...] [--once] [--refresh-interval N] [--limit N] [--state-dir ...]
./agent-status-tui keepalive --cycle-now
```

`monitor` blijft structureel read-only (AST-getest: verwijst nergens naar
`ClaudeKeepalive`/`CodexKeepalive`/`GrokKeepalive`/`KeepaliveAgent`, en
importeert nooit `agentstatus.keepalive.providers`).

### `--cycle-now` (nieuw)

Handmatige productiediagnose: exact één normale, activity-aware cyclus,
via `CycleRunner.run_cycle()` — dezelfde route als de echte `:00`/`:30`-
planning, geen tweede schedulerimplementatie. Boundary = het moment waarop
`--cycle-now` gestart wordt (niet de eerstvolgende `:00`/`:30`). Vaste
agentvolgorde en de bestaande 0/5/10s-stagger blijven exact behouden —
zelfs wanneer een agent SKIP't (het slot zelf wordt nog steeds op tijd
bezocht) en zelfs wanneer een eerdere agent faalt (elke agentfout wordt
per-slot opgevangen, de volgende agent wordt nog steeds op zijn eigen
staggerslot bezocht). Gebruikt dezelfde `on_event`-callback (dus dezelfde
`_record_history`/providerobservatie) als de normale doorlopende cyclus.
Na de ene cyclus sluit het proces af. Wijzigt of raakt de normale
`:00`/`:30`-productieplanning op geen enkele manier aan (geen aanroep naar
`next_boundary()` in dit codepad — structureel getest).

`--once` blijft ongewijzigd: de directe, geforceerde, parallelle
pingdiagnose zonder activity-check.

### Housekeeping (16 dagen) — ongewijzigd

Retentielogica in `agentstatus/keepalive/history.py` is niet aangepast; nog
steeds atomair, 16 dagen, self-healing bij corruptie.

### `LEGACY_INTERVAL_SECONDS` (voorheen `INTERVAL_SECONDS`)

3001 seconden blijft uitsluitend als gedocumenteerde historische constante
in `agentstatus/keepalive/core.py` staan (structureel getest dat niets in
het scheduling-pad er nog naar verwijst). Zie de eerdere revisie van dit
document (git-geschiedenis van `STATUS.md` zelf, indien gewenst) voor de
oorspronkelijke motivatie (`6 * 3001 = 18006s`, 6 keepalive-calls per 5 uur).

### Live validatie (2026-09-20, ~03:24–03:25 lokaal)

Een reële, activity-aware `run_slot()`-aanroep (niet de geforceerde
`--once`) tegen de echte Claude/Codex/Grok-providers, tegen een geïsoleerde
scratch `--state-dir`:

- **Alle drie agents: `SKIP`.** Elk had reële, recente activiteit (Claude
  ~15 min geleden binnen deze sessie; Codex/Grok ~16 min geleden vanuit de
  vorige taak se live-test) — precies zoals verwacht, zonder dat er ook maar
  één providerproces/modelturn is gestart. Dit bevestigt de kernclaim van
  deze taak (minimaliseer tokenverbruik) rechtstreeks, tegen nul kosten.
- Providerobservatie werkte voor alle drie ondanks SKIP: CODEX 1%/2h08m 5h,
  14%/6d07h week; CLAUDE 21%/2h45m 5h, 50%/1d21h week; GROK opnieuw
  `creditUsagePercent: null` (derde onafhankelijke bevestiging sinds de
  vorige taak — zie hieronder).
- `keepalive history`, `keepalive history --agent codex` en `keepalive
  monitor --once` tegen deze data renderen zoals bedoeld (AGENT-kolom
  correct aanwezig/afwezig, ACTION=SKIP, PING=--, reële LAST ACTIVITY).
- `./agent-status-tui doctor` toont `[OK] cycle: every :00/:30, 5s stagger
  per agent`.

**Bijgewerkt na herstart:** de live systemd-service is buiten deze sessie om
herstart en draait sindsdien de nieuwe code (bevestigd via `systemctl`/
`journalctl`: actief sinds 04:56:42, startregel `cycle=:00/:30, stagger=5s`).
Zie "Live productievalidatie (bevestigd)" hieronder voor de daadwerkelijke,
over meerdere reële cycli waargenomen data.

## Live productievalidatie (bevestigd, 2026-09-20 ~05:05)

De live `agent-status-keepalive.service` (systemd --user, herstart door de
gebruiker buiten deze sessie om) heeft inmiddels meerdere echte `:00`/`:30`-
cycli gedraaid op de nieuwe code. Onafhankelijk geverifieerd via
`journalctl`/`keepalive history` tegen de echte state-root
(`~/.local/state/agent-status-tui/keepalive`):

- Cycli om 04:00, 04:30 en 05:00 zijn alle drie zichtbaar in de
  geschiedenis, met de vaste volgorde CLAUDE→CODEX→GROK.
- Stagger is een ondergrens, geen exacte garantie: bij de 05:00-cyclus
  precies `05:00:00 / 05:00:05 / 05:00:10` (0/5/10s, want alle drie
  agents SKIP'ten — snel pad), maar bij de 04:30-cyclus `04:30:00 /
  04:30:05 / 04:30:13` (CODEX pingte echt, wat de daaropvolgende
  GROK-slot enkele seconden later dan +10s liet starten). Dit is
  verwacht/correct gedrag van een sequentiële scheduler, nu expliciet
  gedocumenteerd in `README.md`/`COMMANDS.md` en vastgelegd in een nieuwe
  test (`test_stagger_is_a_floor_a_slow_slot_pushes_later_slots_but_never_earlier`).
- CODEX's SKIP-beslissing om 05:00:05 is met de hand nagerekend: laatste
  activiteit 04:30:12, dus ~1793s vóór het slot — binnen het inclusieve
  1800s-activiteitsvenster, dus terecht SKIP.
- Providerobservatie werkte ook bij SKIP-sloten: de GROK-rij om 05:00:10
  toonde `WEEK 1%` / `RESET 6d13h` ondanks `ACTION=SKIP`. Eerdere cycli
  (04:00, 04:30) toonden voor GROK juist `WEEK --` met de bekende
  `creditUsagePercent unusable/null`-reden — **in dezelfde real-world
  historie dus beide gevallen bevestigd**, wat de Grok-fix hieronder extra
  onderbouwt.
- `./agent-status-tui doctor` toont alle Keepalive-checks `[OK]`,
  inclusief `history: N event(s)` per agent (niet langer `no history yet`).
- Het bestaande `./agent-status-tui`-dashboard toont gelijktijdig
  consistente data met de keepalive-geschiedenis (GROK 5h=n/a, week=1%,
  reset=6d13h, data=LIVE).

Dit bevestigt het volledige productiepad end-to-end: systemd → natuurlijke
cyclusgrens → stagger → activity-beslissing → providerobservatie →
persistentie → history/rendering.

## Grok: `creditUsagePercent: null` — opgelost (2026-09-20, ~04:50–05:15)

De herhaaldelijk geobserveerde live situatie (drie keer in eerdere taken:
`currentPeriod.type` correct `USAGE_PERIOD_TYPE_WEEKLY` met geldige
start/eind, maar `config.creditUsagePercent: null` in de billing-response
zelf) bleek uiteindelijk **wel een concrete verbeterpunt in dit project**,
niet enkel een server-eigenaardigheid om passief te blijven observeren: de
oude `_map_weekly()` (dashboard-adapter, `agentstatus/adapters/grok.py`) en
`_grok_weekly_window()` (keepalive-observer,
`agentstatus/keepalive/observe.py`) gooiden bij een ontbrekend/`null`
percentage het **hele** window weg — inclusief een overigens perfect
bruikbare reset. De officiële Grok-CLI/TUI rendert een ontbrekend
percentage als `0%` (een client-side fallback, geen bewezen serverwaarde);
dit project mag dat expliciet niet overnemen.

**Fix**: percentage en reset zijn nu onafhankelijke velden, precies zoals
`Window` dat al modelleert (`None` = onbekend, nooit nul). Een geldig
weekly `currentPeriod` met een bruikbare `end` maar ontbrekend/`null`/
onbruikbaar `creditUsagePercent` levert nu een `Window(used_percent=None,
resets_at=<echte reset>)` op — percentage toont `--`, reset toont de echte
countdown. Alleen wanneer *beide* onbruikbaar zijn, wordt er (zoals
voorheen) helemaal geen window gerapporteerd. Geen renderer-wijzigingen
nodig: `time_budget()`/`bar()`/keepalive's `render.py` ondersteunden een
`used_percent=None` met geldige `resets_at` al correct. Geen extra
HTTP-request, geen credential-wijziging.

Dashboard en keepalive-observer delen nu dezelfde, expliciet
gedocumenteerde semantiek (zie `README.md`). Live gecontroleerd: het
percentage is momenteel weer gewoon aanwezig (1%) — het `null`-gedrag is
dus kennelijk intermitterend server-side, wat de fix nog relevanter maakt
voor de momenten dat het wél optreedt.

## Doctor

`agentstatus/doctor.py`'s "Keepalive configuration"-sectie toont nu
`check_cycle_configuration()` (was `check_interval_configuration()`):
valideert `CYCLE_SECONDS == 1800` en `AGENT_STAGGER_SECONDS == 5` rechtstreeks
uit `agentstatus.keepalive.core`, i.p.v. het oude `INTERVAL_SECONDS == 3001`.
De "Keepalive history"-sectie (bestandsvaliditeit/retentie/schrijfbaarheid)
is ongewijzigd t.o.v. de vorige taak.

Live `./agent-status-tui doctor`: alle checks `[OK]`, behalve de drie
Keepalive-history-`[WARN]`s ("no history yet") — verwacht zolang de live
service nog niet herstart is.

## Calibrator (gearchiveerd)

Ongewijzigd t.o.v. de vorige taak: volledig uit het actieve product
verwijderd. Zie `docs/calibrator-research.md`.

## Tests

```
python3 -m unittest discover -s tests
```

370 tests, groen (366 ná de vorige taak + 3 Grok percentage/reset-
onafhankelijkheidstests over `tests/test_adapters_grok.py` en
`tests/test_keepalive_history.py` — enkele bestaande Grok-tests die de
oude, inmiddels foutieve "geen percentage => geen window"-aanname
bewaakten zijn aangepast naar de correcte, onafhankelijke semantiek zonder
de kernwaarborg te verzwakken: een ontbrekend percentage wordt nog steeds
nooit `0%` — plus 1 nieuwe test uit de final review voor de
`_do_ping()`-timingfix).

## Projectregels

Zie `AGENTS.md` voor de volledige, bijgewerkte tekst — met name de
keepalive-structuursectie, die nu het klokgebonden model beschrijft i.p.v.
het oude vaste-interval-model.

## Final review (2026-09-20, ~05:05–05:35)

Volledige diff/status/security/documentatie-review vóór de eerste commit
van dit werk. Twee echte, niet-triviale bevindingen zijn gecorrigeerd
(beide met test/regressiedekking):

- **Timing-bug in `_do_ping()`** (`agentstatus/keepalive/core.py`): de
  `finished`-timestamp werd vóór de aanroep van `provider.ping()` gelezen
  in plaats van erna, waardoor `last_ping_finished_at` (persisted state)
  en het `TIME`-veld van een PING-history-event het moment waarop de ping
  *begon* registreerden, niet waarop hij *eindigde* — tot 120s af (de
  ping-timeout) bij een langzame ping. Beïnvloedde de scheduling zelf niet
  (die is zuiver klokgebonden, leest dit veld nooit terug), maar wel de
  nauwkeurigheid van de gerapporteerde tijd. Gefixt: `_do_ping()` leest de
  klok nu pas na `provider.ping()`. Nieuwe test:
  `test_stagger_is_a_floor_a_slow_slot_pushes_later_slots_but_never_earlier`.
- **Verouderde documentatie/comments** die nog verwezen naar de oude
  `KeepaliveAgent._floor()`/`_wait()`/`.ping()`-methoden en
  `INTERVAL_SECONDS` uit het vóór-klokgebonden model (`observe.py`,
  `doctor.py`, `grok_billing.py`, een testcomment) — feitelijk onjuist na
  de scheduler-herschrijving. Gecorrigeerd naar de huidige architectuur;
  geen gedragswijziging.
- README.md/COMMANDS.md preciseerden de stagger als "5 seconden" zonder te
  vermelden dat dit een ondergrens is, geen exacte garantie — nu expliciet
  vermeld, mede op basis van de live-geobserveerde 04:30-cyclus (zie
  hierboven).

Geen andere blockers, regressies, of beveiligingsproblemen gevonden. Zie
FINAL REPORT (buiten dit document) voor het volledige overzicht.

## Open / te bewaken

- Langdurig gedrag van het nieuwe :00/:30-schema over een nog langere
  periode (dagen) blijft de moeite waard om te blijven observeren —
  meerdere reële cycli zijn nu bevestigd correct (zie hierboven), maar dit
  is geen vervanging voor voortgezette normale operationele observatie.
- Grok's `creditUsagePercent: null`-situatie is **opgelost en live
  bevestigd** (zie hierboven) — geen open punt meer.

## NEXT STEP

Dit werk is klaar om gecommit te worden (zie FINAL REPORT). Na de commit:

1. Blijf de live keepalive-geschiedenis periodiek steekproefsgewijs
   controleren (`./agent-status-tui keepalive history`), met name rond
   agents die daadwerkelijk pingen (niet alleen skippen) om te bevestigen
   dat de timestamp-fix zich ook onder reële trage pings correct gedraagt.
2. Bij twijfel over een toekomstige wijziging: `AGENTS.md` → `STATUS.md` →
   code/tests, zoals gebruikelijk.

## Nieuwe werksessie

`AGENTS.md` → `STATUS.md` → `README.md`/`COMMANDS.md` → code/tests/git-history
voor taakspecifieke details.
