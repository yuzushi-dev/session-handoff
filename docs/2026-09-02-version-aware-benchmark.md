# Benchmark version-aware: session-handoff 0.6.1 vs 0.7.0

Data: 2026-09-02

## Esito

Il benchmark deterministico è valido: entrambe le build hanno manifest
coerenti, tutti gli scenari terminano senza errore inatteso e il runner registra
`provider_calls: 0`. Risultato completo locale (ignorato da Git insieme agli
altri artifact di run):
`benchmark/results-version/2026-09-02-0.6.1-vs-0.7.0.json`.

| Build | Commit | Tree hash tracciato |
|---|---|---|
| 0.6.1 | `8318b93b0a1c71553971461aea45d1418b6f136d` | `f4508cbe02e51e8d84ccb15fd7d9243e3e080ab750232020da9de17e8e7c65eb` |
| 0.7.0 | `fa549da9c35ff9aabfe8f44b32309b17f4fdd41f` | `b0f75c15ff39733e1ae2c4e3cd6627f3d85cb8af62676dedd28fc9d9a7440e52` |

## Osservazioni

| Scenario | 0.6.1 | 0.7.0 |
|---|---|---|
| Primo `SessionStart` | reminder legacy; stato `unasked` | reminder in-chat; stato `asked` |
| Secondo `SessionStart` | reminder ripetuto | `{}`; prompt già rivendicato |
| Risposta `yes` | capability assente; stato `unasked` | hook presente; stato `enabled` |
| Risposta `no` | capability assente; stato `unasked` | hook presente; stato `declined` |
| Config legacy + `telemetry enable` non interattivo | exit `2` | exit `0`, consenso pending |
| `DO_NOT_TRACK` | disabilitato | disabilitato |

Le differenze sono state mantenute come evidenza (`different` o
`unsupported`), non trasformate in uno score unico. Il confronto conferma che
0.7.0 introduce il workflow di consenso persistente e l’hook
`UserPromptSubmit`; non misura qualità del modello, context rot o fedeltà di un
handoff.

## Riproduzione

```bash
python3 benchmark/version_aware.py \
  --build 0.6.1=/path/to/session-handoff-0.6.1 \
  --build 0.7.0=/path/to/session-handoff-0.7.0 \
  --output benchmark/results-version/0.6.1-vs-0.7.0.json
```

Implementazione e contratto: [version_aware.py](../benchmark/version_aware.py) e
[benchmark/README.md](../benchmark/README.md).

## Lane provider-backed del pilot

È stata eseguita anche una lane live separata sul caso
`late-correction/long/r02`: 2 chiamate Markdown su `0.6.1` e 2 su `0.7.0`.
Entrambe hanno avuto `task_success`, acceptance e DoD `2/2`, con fedeltà
esplorativa uguale dei tre fatti critici. Il risultato non è un quality score
di release: una sola fixture e un runner legacy senza
`--reasoning-effort` non consentono di attribuire i delta alla versione.

Il confronto `Markdown` vs `state-v1` è stato eseguito separatamente sul build
moderno, perché `0.6.1` non espone `state-v1`. Risultati e limiti sono nel
[pilot aggregato](./2026-09-02-real-context-rot-pilot.md).
