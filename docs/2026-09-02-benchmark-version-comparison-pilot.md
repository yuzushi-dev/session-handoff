# Benchmark confronto versioni — pilot 2026-09-02

Status: evidenza esplorativa; non è un verdetto di release.

## Scope

Il pilot segue `docs/plans/2026-09-01-benchmark-version-comparison.md` e usa il
runner moderno pulito al commit `d7e48af19201cc3bde12e187202e6c11526ad785`.
Per contenere il costo sono state eseguite una sola coppia di celle:

- caso `superseded-decision`;
- banda `long`;
- replica `1`;
- client Codex CLI `0.152.0`;
- modello `gpt-5.6-luna`;
- reasoning effort `high`;
- ordine arm: `markdown-v1`, poi `state-v1`;
- 4 chiamate provider totali.

Artifact grezzi locali: `/tmp/session-handoff-version-benchmark-5xAKKN/results-modern-head-d7e48af`.

## Risultato automatico

| Formato | Stato | Chiamate | Contesto | Input token | Output token | Wall time | Task success | Trace failures |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `markdown-v1` | completed | 2 | 1.100 B | 94.088 | 1.017 | 33,912 s | true | 0 |
| `state-v1` | completed | 2 | 1.662 B | 94.386 | 1.050 | 35,481 s | true | 0 |

Delta `state-v1 - markdown-v1`:

- contesto: **+562 B (+51,1%)**;
- input: **+298 token (+0,3%)**;
- output: **+33 token (+3,2%)**;
- wall time: **+1,569 s (+4,6%)**.

Entrambe le celle hanno superato verifica e acceptance nascosta. I contatori
semantici di fatti e stale trap sono ancora null: una singola coppia non basta
per una decisione di adozione.

## Aggiornamento live: lane release

Il confronto live è stato poi eseguito sul formato comune Markdown, usando il
runner presente in ciascun ref e 2 chiamate per release. Fixture, modello,
replica e prompt sono rimasti fissi; sono state 4 chiamate provider nuove.

| Release | Handoff | Input token | Output token | Wall time | Task success | DoD | Acceptance |
|---|---:|---:|---:|---:|---|---:|---|
| `0.6.1` | 1.672 B | 165.688 | 1.982 | 56,346 s | sì | 2/2 | pass |
| `0.7.0` | 1.302 B | 150.839 | 1.836 | 53,952 s | sì | 2/2 | pass |

Entrambe le release hanno preservato i tre fatti critici e non hanno introdotto
uso dello stale endpoint. Il delta descrittivo osservato in `0.7.0` non è una
prova di superiorità: è una sola fixture, l'output è stocastico e il runner
legacy non registra né accetta `--reasoning-effort`. Artifact:
`/tmp/session-handoff-version-benchmark-5xAKKN/version-results-0.6.1` e
`/tmp/session-handoff-version-benchmark-5xAKKN/version-results-0.7.0`.

## Nota di comparabilità

Il runner importa il proprio `server/` e non installa né invoca il package sotto
test; tra i due ref cambiano soprattutto telemetry e metadata. La lane live
misura quindi una regressione del percorso Markdown sul caso scelto, mentre il
benchmark deterministico misura direttamente le differenze release-specifiche
di consenso e hook. I due risultati vanno letti separatamente.

`0.6.1` non supporta `state-v1`: un 2×2 release×formato completo richiederebbe
un backport o un nuovo build e non sarebbe più un confronto puro tra release.
La superiorità Markdown/state è stata quindi testata nel build moderno, con il
risultato aggregato in [questo pilot](./2026-09-02-real-context-rot-pilot.md).

## Prossimo passo

La matrice completa Markdown/state ha 72 celle
(`6 casi × 3 band × 2 formati × 2 repliche`) e richiede 144 chiamate provider
(generazione più continuazione per cella), prima dei controlli `full`, `migrate`
e `oracle`. Il pilot corrente supporta Markdown come default esplorativo e non
autorizza una conclusione di superiorità statistica né di release.
