# Preflight di pubblicazione — Opus high — 2026-09-08

**Verdetto: NO-GO allo stato attuale.** Le verifiche locali sono passate;
restano correzioni e preparazione della release.

## Provenienza della review

Aggiornamento successivo: su autorizzazione dell’utente, Luna xhigh ha risolto
B4 nei due report dei pilot: riferimenti ai raw convertiti in percorsi locali
senza hyperlink, disponibilità locale e mancata verificabilità pubblica esplicite.
Numeri e hash preservati; link documentali residui e diff-check verificati.
Gli altri rilievi e gate restano aperti. Il report originale di Opus sotto è
conservato come fotografia precedente a questo fix.

- Richiesta esplicita dell’utente: Opus, reasoning high.
- Claude Code 2.1.263, `--model opus --effort high`; modello della review
  confermato dagli eventi: `claude-opus-5`, senza fallback configurato.
  Il client rendiconta anche uso ausiliario di `claude-haiku-4-5-20251001`;
  le risposte di review sono di Opus.
- Modalità safe/restricted, soli strumenti Read/Glob/Grep, nessun MCP,
  hook, Bash, Edit o Write del reviewer. Una review iniziale e un aggiornamento
  finale con le nuove prove del coordinatore.
- Copia isolata di 142 file del working tree corrente, inclusi gli untracked
  pertinenti; root `plugin.json` escluso perché già rimosso. Nessuna modifica
  a codice, indice o commit originali durante la verifica.
- HEAD di partenza: `b5bee57ec5b5063e7c68eabdf5ad2a2a1be9ea2f`; branch `feature/handoff-search-loss-contract`.
- Evidenze native e log locali: `/tmp/session-handoff-opus-preflight-jc4o3frc`.

## Verifiche eseguite dal coordinatore

| Controllo | Risultato |
| --- | --- |
| Suite completa Python 3.12 sullo snapshot con indice coerente | **999 passed, 3 skipped**, 192.01 s |
| Otto test di provenienza prima falliti nell’indice originale | **8 passed**, 19.86 s nello snapshot |
| Ruff completo, incluso `bin/session-handoff` | PASS |
| `compileall bin server hooks` | PASS |
| JSON/versioni/assert README della CI | 14 JSON validi; 4 versioni 0.7.2 coerenti; 3 assert PASS |
| `npm pack --dry-run --ignore-scripts` | PASS, 29 file, 88809 byte |
| Stabilità del codice durante review | Tutti i 142 file uguali allo snapshot; HEAD invariato; indice originale senza staging |
| Riproduzione digest mancante | Digest `null` prima e dopo modifica di un altro file tracciato; validatore accetta |

La suite è stata eseguita con HOME temporanea e dipendenze Python già installate
rese disponibili via PYTHONPATH, senza installare pacchetti. Il primo run isolato
aveva due override introdotti dal coordinatore (`SESSION_HANDOFF_HOME` e
`SESSION_HANDOFF_SKIP_TELEMETRY_PROMPT`) incompatibili con le assunzioni dei test
degli hook: quei 10 fallimenti non sono regressioni del prodotto. Un successivo
avvio non trovava pytest dopo l’isolamento di HOME e non ha eseguito test.
Il run finale con ambiente corretto è quello completo verde riportato sopra.
Un test di timeout produce un BrokenPipeError nel server di prova; pytest termina
con exit 0. Non sono stati eseguiti nuovi benchmark a pagamento.

La riproduzione di provenienza usa una fixture Git isolata con un file tracciato
mancante. Il calcolo del digest e il validatore sono quelli reali; gli altri
componenti della provenienza sono mantenuti immutabili tramite mock. Conferma un
difetto nel controllo dei benchmark, non una vulnerabilità del runtime del plugin.

Controlli remoti in sola lettura:

- [GitHub v0.7.2](https://github.com/yuzushi-dev/session-handoff/releases/tag/v0.7.2)
  già pubblicata il 3 settembre 2026, 13:40:59 UTC.
- [Registry npm](https://registry.npmjs.org/session-handoff/latest): latest/stable 0.7.0.
- I cataloghi [Claude](https://github.com/yuzushi-dev/yuzushi-plugins/blob/main/.claude-plugin/marketplace.json)
  e [Codex](https://github.com/yuzushi-dev/yuzushi-plugins/blob/main/.agents/plugins/marketplace.json)
  puntano ancora a `v0.7.0`.
- [Ultima CI verde osservata](https://github.com/yuzushi-dev/session-handoff/actions/runs/33762401361)
  su `6bcc4e7bae4af4c7be2e42308f7afb9495f1b61f`, non sul candidato corrente.

## Precisazioni operative del coordinatore

- I comandi di staging suggeriti da Opus sotto **non sono stati eseguiti**.
  Il working tree contiene lavoro preesistente: preparare una release con scope
  esplicito, preferibilmente in un checkout separato; non fare un `git add -A`
  indiscriminato del repository originale.
- La necessità di una nuova versione è confermata; `0.7.3` è la proposta di Opus,
  non una versione già assegnata. Non sovrascrivere il tag pubblico `v0.7.2`.
- La suite locale completa e compileall sono ora verdi; resta necessaria CI sul
  commit effettivamente destinato alla release, dopo le ulteriori correzioni.
- Python 3.10, macOS e onboarding nativo sul candidato finale non sono stati
  riesercitati in questo preflight. Il gap di copertura non dimostra un difetto.
- La documentazione telemetria conserva il gate della canary di sette giorni.
  La review non lo revoca: completamento e prove packet-to-storage restano da
  dimostrare prima di dichiarare soddisfatti tutti i gate di release.
- Commenti in italiano e margine teorico del timeout non sono blocker di publish.
- Sul rilievo B4, il report Claude contiene già la dicitura “Raw evidence, stored
  locally”. Il problema residuo è l'accessibilità pubblica dei file citati e
  l'uso di hyperlink verso destinazioni escluse da Git, non l'assenza di quella
  dichiarazione né la dimostrazione che manchino nuovi esperimenti. Opus conferma
  la coerenza dei numeri README con i report; non ha ispezionato le prove grezze.

Nessun fix del prodotto, commit, tag, push, publish, installazione personale,
deploy o nuovo handoff eseguito in questo preflight. La dashboard era già stata
deployata nel task precedente, su approvazione distinta.

---

## Report finale di Opus (testo originale)

# VERDETTO FINALE aggiornato — session-handoff, candidato 2026-09-08

**Esito: NO-GO sull'albero nello stato attuale → GO condizionato dopo i gate G1–G3.**
Nessuna esecuzione da parte mia: solo letture. Le prove di suite completa, Ruff, `compileall`, `npm pack` e il reproducer di provenienza provengono dal `FINAL-ADDENDUM.md` del coordinatore (esecuzione sua, non mia). Percorsi relativi a `workspace/`.

## Cosa cambia rispetto al mio preflight precedente

- **B2 chiuso in locale.** Suite completa sull'intero candidato: 999 passed, 3 skipped, 192 s, Python 3.12; Ruff fresco, `compileall`, 14 JSON snapshot validi, 3 assert README della CI, `npm pack --dry-run` 29 file / 88809 byte (`FINAL-ADDENDUM.md:2-3`). Le 8+10 failure precedenti sono spiegate come artefatti di harness/indice, non regressioni di prodotto: **non le mantengo aperte**. Resta aperto solo il **CI remoto**.
- **B5 rafforzato**: riprodotto in fixture Git isolata (`FINAL-ADDENDUM.md:4`).
- **B3 confermato dall'esterno**: release GitHub v0.7.2 già pubblicata il 2026-09-03 (`FINAL-ADDENDUM.md:5`), e `CHANGELOG.md:3` è ancora `0.7.2 — 2026-09-03` (verificato da me).
- **B6/B7/B8 declassati**: non sono blocker.

## Blocker reali (bloccano il tag/publish)

**B1 — L'albero committabile non corrisponde al candidato testato (indice/release-prep).**
`git-status.txt:14` mostra ` D plugin.json` in colonna worktree, non index; inoltre ~40 path sono **untracked**, fra cui `server/onboarding.py` (`git-status.txt:60`) e `docs/portable/` (`git-status.txt:59`), che è una delle 4 fonti di versione validate dalla CI. Un `git commit` senza `git rm` + `git add -A` pubblicherebbe un albero con il manifest di root ancora presente e **senza il modulo di onboarding** usato da `hooks/session-start.py`. Il coordinatore ha misurato la suite su un indice temporaneo coerente, quindi il *contenuto* è sano: il difetto è che quell'indice non è stato committato (`FINAL-ADDENDUM.md:2`). Limite di prova onesto: l'effetto del manifest di root è dimostrato solo per Codex (`docs/2026-09-08-native-onboarding-check.md:24`), non per entrambi i client. **Certezza alta**, correzione meccanica.

**B3 — Riuso di 0.7.2 per contenuto diverso, CHANGELOG non aggiornato.**
`package.json:3`, `.claude-plugin/plugin.json:4`, `.codex-plugin/plugin.json:3`, `docs/portable/plugin.json:4` = `0.7.2` (confermati identici in `FINAL-ADDENDUM.md:3`) contro `CHANGELOG.md:3` (`0.7.2 — 2026-09-03`, senza central store, manifest nativi, onboarding hook, attribuzione esiti). Con la release v0.7.2 già pubblica, due codebase distinti condividerebbero lo stesso `plugin_version` (`server/version.py:26`) e le righe Loki 0.7.2 del 6-8 settembre diventerebbero non separabili nel pannello per versione (`backend/grafana-dashboard-session-handoff.json:400`). npm resta a 0.7.0 e i manifest marketplace puntano `v0.7.0` (`FINAL-ADDENDUM.md:5`): il bump è anche l'unico modo per far vedere l'upgrade. **Certezza alta.**

**B5 — `_repository_sha256` fail-open silenzioso.**
Con un file tracciato assente, `benchmark/run_study.py:289-292` cattura `OSError` e ritorna `None`; a `benchmark/run_study.py:360` il confronto diventa `None != None` → falso e il gate "repository changed after pair provenance was recorded" passa senza verificare nulla. Riprodotto indipendentemente in fixture isolata, prima e dopo modifica di un altro file tracciato (`FINAL-ADDENDUM.md:4`, `provenance-reproducer.json`). Non è un path di prodotto runtime, ma invalida la provenienza dei benchmark a pagamento citati come evidenza. Fix minimo: eccezione esplicita invece di `None`, e `None` trattato come errore alla riga 360. **Certezza alta.**

**B4 — Evidenza grezza dei pilot non pubblicabile.**
`README.md:22` rinvia ai pilot; `docs/2026-09-08-claude-information-pilot.md:84-85` punta a `../benchmark/results-version/...` escluso da `.gitignore:6`; le identità del pilot Codex sono path `/tmp`/`~/.codex` (`docs/2026-09-08-information-preservation-pilot.md:120-126`). Link 404 su GitHub sotto un claim di prima riga ("zero critical facts lost"). Confermato in `FINAL-ADDENDUM.md:6`. Fix minimo: committare i risultati blinded **oppure** dichiarare "evidenza locale, non pubblicata" e rimuovere i link. **Certezza alta.** I numeri README restano corretti.

## Condizioni operative e rischi non bloccanti (separati)

1. **CI remoto non copre il candidato**: l'ultimo run verde è `6bcc4e7`, non questo albero (`FINAL-ADDENDUM.md:3`); `ci.yml:3-8` non scatta sul branch corrente senza PR o `workflow_dispatch`. Nessun claim "CI verde" è lecito finché non gira.
2. **Gap di copertura, non difetti**: smoke Codex a 0.151.0 (`ci.yml:40-45`) contro 0.153.4 dei claim (`README.md:56`); solo `ubuntu-latest` (`ci.yml:20,79`) contro macOS dichiarato (`package.json:32-35`). Nessun difetto macOS dimostrato.
3. **Python 3.10**: nessuna API 3.11+ trovata per grep, ma la suite è girata solo su 3.12. Rischio residuo basso, non verificato.
4. **B7 margine timeout** (`server/checkpoint.py:27-28` budget 4.5 s vs `hooks/hooks.json:24` timeout 5 s): aritmetica plausibile, **non misurata**; fail-open dichiarato. Raccomandato, non bloccante.
5. **B8 commento in italiano** (`hooks/user-prompt-submit.py:26-27`): cosmetico.
6. **Attribuzione e avvisi one-shot** (`server/setup.py:369`, `server/onboarding.py:44-48`): sotto-conteggio conservativo, coerente con la documentazione.
7. **Canary telemetria 7 giorni**: completamento e arrivo pacchetto→storage restano non verificati (`FINAL-ADDENDUM.md:6`); un pannello vuoto non implica raccolta rotta (`docs/telemetry.md:32-41`).
8. Un `BrokenPipeError` del test-server durante un test di timeout di rete, con `pytest` a exit 0 (`FINAL-ADDENDUM.md:2`): rumore, da tenere d'occhio.

## Gate minimi residui

- **G1**: `git rm plugin.json` + `git add -A` (inclusi `server/onboarding.py` e `docs/portable/`); `git status` pulito; commit.
- **G2**: bump a **0.7.3** nei 4 manifest + nuova voce CHANGELOG (rimozione manifest di root, central store ref-first, onboarding hook, attribuzione esiti).
- **G3**: fix B5 + rerun `tests/test_run_study.py`; risoluzione B4 (commit evidenza o disclaimer + link rimossi).
- **G4 (post-commit, prima del tag)**: rieseguire suite/Ruff/`compileall`/`npm pack` **su quell'esatto commit** e far girare la **CI remota** via PR o `workflow_dispatch`; allineare `CODEX_RELEASE` a 0.153.4 e ripetere l'onboarding nativo sul candidato finale; opzionale ma consigliato un job `macos-latest` o attenuazione del claim macOS.

Sciolti G1–G3 con G4 verde, il mio giudizio passa a **GO**, con i limiti dichiarati sopra (nessuna prova su Python 3.10/macOS, canary non chiuso).
