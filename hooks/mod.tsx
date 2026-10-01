// Claude Code mod (loaded via hooks/claude-mod.json): a thin layer over the
// lifecycle scripts (session-start, user-prompt-submit, checkpoint,
// compact_advisor). The logic stays in Python; this file spawns the scripts,
// marks the classic payload so they don't run twice, and draws their output.
// Codex and Claude < 2.1.287 keep using the classic hooks in hooks/hooks.json.
import { atom, read, update } from 'claude-code'
import type { Hook, ProcessRunResult, Register } from 'claude-code'

import type { SessionHandoffAdvice, SessionHandoffTranscript } from '../types'

// Keep in sync with MOD_ACTIVE_FIELD in server/compact_advisor.py.
const MOD_ACTIVE_FIELD = 'session_handoff_mod'
const ADVISOR_TIMEOUT_MS = 5_000 // same budget as the classic Stop hook
// Same budgets as hooks/hooks.json.
const SESSION_START_TIMEOUT_MS = 5_000
const PRE_COMPACT_TIMEOUT_MS = 5_000
const PROMPT_SUBMIT_TIMEOUT_MS = 1_000
// The classic SessionStart matcher: `fork` never ran the script.
const SESSION_START_SOURCES = ['startup', 'resume', 'clear', 'compact']

type Api = Parameters<Hook<'classic.Stop'>>[0]

type Script = {
  // True when the process was launched and finished (any exit code) or was
  // killed on the timeout: it ran, so re-running it could repeat its effects.
  // False when the spawn failed or was refused: nothing ran.
  ran: boolean
  // The result of a clean exit (code 0), else null.
  run: ProcessRunResult | null
}

// `$.process.run` rejects both for a command that cannot start (ENOENT, a hook's
// `deny`) and for one killed on the timeout; only the latter says "still running".
const TIMED_OUT = /still running after/

const stripMarker = (e: object): Record<string, unknown> => {
  const { [MOD_ACTIVE_FIELD]: _marker, ...rest } = e as Record<string, unknown>
  return rest
}

// Spawns a lifecycle script with the hook's original stdin (marker removed) and
// the host variables a classic command hook gets. Never throws.
const runScript = async ($: Api, script: string, e: { cwd?: string; session_id?: string }, timeoutMs: number): Promise<Script> => {
  try {
    const run = await $.process.run(['python3', `${$.plugin.root}/${script}`], {
      cwd: e.cwd ?? (await $.session.cwd()),
      env: e.session_id === undefined ? {} : { CLAUDE_CODE_SESSION_ID: e.session_id },
      stdin: JSON.stringify(stripMarker(e)),
      timeoutMs,
    })
    return { ran: true, run: run.exitCode === 0 ? run : null }
  } catch (error) {
    return { ran: TIMED_OUT.test(error instanceof Error ? error.message : String(error)), run: null }
  }
}

// The classic hook is the fallback: the marker goes on only after the script ran.
const marked = <T extends object>(e: T, script: Script): T =>
  script.ran ? ({ ...e, [MOD_ACTIVE_FIELD]: true } as T) : e

type HookOutput = { systemMessage?: string; additionalContext?: string }

// The scripts print the classic hook JSON: systemMessage and
// hookSpecificOutput.additionalContext, nothing else is read.
const parseHookOutput = (stdout: string): HookOutput => {
  try {
    const parsed = JSON.parse(stdout.trim()) as {
      systemMessage?: unknown
      hookSpecificOutput?: { additionalContext?: unknown }
    } | null
    const message = parsed?.systemMessage
    const context = parsed?.hookSpecificOutput?.additionalContext
    return {
      ...(typeof message === 'string' && message !== '' ? { systemMessage: message } : {}),
      ...(typeof context === 'string' && context !== '' ? { additionalContext: context } : {}),
    }
  } catch {
    return {}
  }
}

const adviceRef = { plugin: 'session-handoff', key: 'advice' } as const
const advice = atom(adviceRef, null)
const transcript = atom({ plugin: 'session-handoff', key: 'transcript' } as const, null)

const parseAdvice = (stdout: string): SessionHandoffAdvice | null => {
  try {
    const parsed: unknown = JSON.parse(stdout.trim())
    const text = (parsed as { systemMessage?: unknown } | null)?.systemMessage
    return typeof text === 'string' && text !== '' ? { text } : null
  } catch {
    return null
  }
}

export const register: Register = on => {
  const remember = (e: { session_id: string; transcript_path: string }): SessionHandoffTranscript => ({
    sessionId: e.session_id,
    path: e.transcript_path,
  })

  // The mod has no API for the transcript path; the classic events carry it.
  // The mod runs the script itself and, once it has run, marks the payload so the
  // classic hook (session-start.py) exits early. If the spawn failed or was
  // refused the payload goes down unmarked and the classic hook is the fallback
  // (it stays the only path for Codex and older Claude Code). The script's
  // systemMessage (launcher notice, telemetry consent) becomes a transcript line
  // (`$.ui.log`: not sent to the model, as systemMessage is not) and its
  // additionalContext (compaction recovery) joins the result.
  on('classic.SessionStart', async ($, e, next) => {
    await update($, transcript, () => remember(e))
    if (!SESSION_START_SOURCES.includes(e.source)) return next(e)
    const script = await runScript($, 'hooks/session-start.py', e, SESSION_START_TIMEOUT_MS)
    const result = await next(marked(e, script))
    const out = script.run === null ? {} : parseHookOutput(script.run.stdout)
    if (out.systemMessage !== undefined) $.ui.log(out.systemMessage)
    if (out.additionalContext === undefined) return result
    return { ...result, additionalContext: [...(result.additionalContext ?? []), out.additionalContext] }
  })

  // The script's only effect is recording an exact consent reply; it never
  // changes the prompt.
  on('classic.UserPromptSubmit', async ($, e, next) => {
    await update($, transcript, () => remember(e))
    const script = await runScript($, 'hooks/user-prompt-submit.py', e, PROMPT_SUBMIT_TIMEOUT_MS)
    return next(marked(e, script))
  })

  // checkpoint.py prints `{}` and reports a failure on stderr only, so a clean
  // exit with empty stderr is the success signal for the toast.
  on('classic.PreCompact', async ($, e, next) => {
    const script = await runScript($, 'server/checkpoint.py', e, PRE_COMPACT_TIMEOUT_MS)
    const result = await next(marked(e, script))
    if (script.run !== null && script.run.stderr.trim() === '') $.ui.toast('session-handoff: checkpoint saved')
    return result
  })

  // The advisor runs here, in the Stop hook itself, not on turn.complete: the
  // classic Stop hook is marked only after the mod ran the advisor, so it can
  // never be silenced without the mod's own run (and never doubles: a second
  // run would be a second TypeSafe call and a second message). Opt-in unset:
  // nothing is spawned and the payload goes down unmarked; the classic script
  // reads the same variable and exits quietly. Always continue the chain so
  // other plugins' Stop hooks still run.
  on('classic.Stop', async ($, e, next) => {
    await update($, transcript, () => remember(e))
    let optedIn = false
    try {
      optedIn = ((await $.env.get('SESSION_HANDOFF_COMPACT_HINT')) ?? '').trim() !== ''
    } catch {
      // Unreadable env reads as unset: the classic hook decides.
    }
    if (!optedIn) return next(e)

    const before = await $.state.get(adviceRef)
    const script = await runScript($, 'server/compact_advisor.py', e, ADVISOR_TIMEOUT_MS)
    if (script.ran) {
      try {
        const found = script.run === null ? null : parseAdvice(script.run.stdout)
        // Publish only for the session that asked, and only if nothing reset
        // the advice meanwhile (prompt.submit, session.end bump its version).
        if (found !== null && (await $.session.id()) === e.session_id) {
          const write = await $.state.set(adviceRef, found, { ifVersion: before.version })
          if (write.isSet) $.ui.toast(found.text)
        }
      } catch {
        // Fail open, like the classic hook: no hint on any error.
      }
    }
    return next(marked(e, script))
  })

  on('prompt.submit', async ($, e, next) => {
    await update($, advice, () => null)
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await update($, advice, () => null)
    await update($, transcript, () => null)
    return next(e)
  })

  // Draw the hint above the engine's (and other plugins') own band, not instead
  // of it: `next(e)` is their tree, kept beneath the hint row.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, advice)
    if (current === null || e.props.hasSurvey) return next(e)

    const beneath = await next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Box>
          <Text dimColor>{'◇ '}{current.text}{' '}</Text>
          <Button key="dismiss" label="Dismiss" onPress={() => update($, advice, () => null)} />
        </Box>
        {beneath}
      </Box>
    )
  })
}
