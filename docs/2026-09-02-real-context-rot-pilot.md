# Pilot reale context-rot/handoff: versioni e formato

Data: 2026-09-02

## Setup

- Fixture held-out: `late-correction`, band `long`, replica `2`.
- Lane formato: build moderna pulita `d7e48af19201cc3bde12e187202e6c11526ad785`;
  bracci `full`, `oracle`, `handoff/markdown-v1`, `handoff/state-v1`.
- Lane release: build `0.6.1` (`8318b93b`) e `0.7.0` (`fa549da9`),
  formato comune `Markdown`.
- Client: Codex CLI `0.152.0`; modello `gpt-5.6-luna`.
- La lane formato usa reasoning `high`. Il runner delle release è precedente e
  non espone `--reasoning-effort`; usa la stessa invocazione/default profile per
  entrambe le release. I costi assoluti delle due lane non sono quindi pooled.
- Chiamate nuove aggregate: `10` (`6` lane formato + `4` lane release).
  Nessun risultato precedente riusato.

Artifact completi:

- lane formato: `/tmp/session-handoff-version-benchmark-5xAKKN/real-results-late-correction`;
- release `0.6.1`: `/tmp/session-handoff-version-benchmark-5xAKKN/version-results-0.6.1`;
- release `0.7.0`: `/tmp/session-handoff-version-benchmark-5xAKKN/version-results-0.7.0`.

## Risultati automatici

| Braccio | Contesto fornito | Input token | Output token | Wall time | Task success | DoD | Trace failures |
|---|---:|---:|---:|---:|---|---|---:|
| `full` | 160.119 B | 175.273 | 636 | 27,506 s | sì | 2/2 | 0 |
| `oracle` | 488 B | 50.745 | 431 | 21,421 s | sì | 2/2 | 0 |
| `markdown-v1` | 1.339 B | 94.252 | 1.080 | 38,797 s | sì | 2/2 | 0 |
| `state-v1` | 1.975 B | 95.116 | 1.195 | 41,316 s | sì | 2/2 | 0 |

## Fedeltà semantica dell’handoff

Revisione esplorativa contro i gold facts della fixture:

| Metrica | Markdown | State |
|---|---:|---:|
| Recall fatti critici | 1,00 | 1,00 |
| Fatti incorretti | 0 | 0 |
| Intrusione stale | 0 | 0 |
| DoD pass rate | 1,00 | 1,00 |

Entrambi gli handoff hanno mantenuto la correzione `/api/v2/exports`, il file
di implementazione e il test da aggiornare. Nessuno ha trattato
`/api/v1/export` come endpoint corrente.

## Lane release: confronto 0.6.1 vs 0.7.0

Il confronto live usa il solo formato supportato da entrambe le release, con
una chiamata di generazione e una di continuazione per release:

| Release | Handoff | Input token | Output token | Wall time | Task success | DoD | Acceptance |
|---|---:|---:|---:|---:|---|---:|---|
| `0.6.1` | 1.672 B | 165.688 | 1.982 | 56,346 s | sì | 2/2 | pass |
| `0.7.0` | 1.302 B | 150.839 | 1.836 | 53,952 s | sì | 2/2 | pass |

Delta descrittivo `0.7.0 - 0.6.1`: `-370 B` di handoff (`-22,1%`), `-14.849`
input token (`-9,0%`), `-146` output token (`-7,4%`) e `-2,394 s` (`-4,3%`).
Entrambi gli handoff hanno recall 1,00 dei tre fatti critici, zero fatti
incorretti e zero intrusione stale nella revisione esplorativa.

Questa differenza non è attribuibile alla release: il task e il percorso di
handoff sono uguali, la fixture è una sola, e l'output è stocastico. La lane
mostra assenza di regressione live osservata sul caso scelto, non la superiorità
qualitativa di `0.7.0`. La differenza release-specifica osservata dal benchmark
version-aware resta quella deterministica di consenso/hook descritta in
[questo report](./2026-09-02-version-aware-benchmark.md).

## Confronto Markdown vs state-v1

Sul build moderno, a parità di fixture e modello, entrambi i formati hanno
superato task, DoD e acceptance e hanno preservato tutti i fatti critici.
`state-v1` non ha mostrato un vantaggio semantico; ha invece aggiunto 636 B di
contesto (`+47,5%`), 864 input token (`+0,9%`), 115 output token (`+10,7%`) e
2,520 s (`+6,5%`) rispetto a Markdown.

La decisione del pilot è quindi: Markdown resta il default esplorativo; state-v1
è corretto e senza regressioni nel caso osservato, ma non superiore. Il caso
`full` ha anch'esso avuto successo, perciò il pilot non forza una failure da
context rot e non dimostra un miglioramento generale dell'handoff.

## Interpretazione

Su questo caso entrambi i formati comprimono drasticamente il contesto senza
perdere informazione utile: Markdown riduce i byte forniti del 99,16% rispetto
a `full`, state del 98,77%. State-v1 costa però 636 B in più di contesto
(+47,5%), 864 input token (+0,92%), 115 output token (+10,65%) e 2,52 s
(+6,49%) rispetto a Markdown, senza un beneficio semantico osservato.

Il caso non produce una failure da context rot: anche `full` raggiunge il
risultato corretto. Quindi dimostra compressione efficace e parità di esito,
non una superiorità generale dell’handoff né un vantaggio di state-v1.

## Limiti e decisione

È un pilot esplorativo: un solo caso, una replica per braccio e revisione
semantica manuale non condition-blind/calibrata. Non supera il release gate e
non giustifica significatività statistica. Inoltre `0.6.1` non supporta
`state-v1`, quindi un 2×2 release×formato completo non sarebbe un confronto
delle release: la lane release usa correttamente Markdown, mentre la lane di
superiorità usa il build moderno.

Servono più casi e repliche per misurare quando l'handoff evita davvero errori
da context rot e se un formato strutturato produce un beneficio semantico
ripetibile.
