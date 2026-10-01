import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const PLUGIN = 'session-handoff'
const TRANSCRIPT = '/tmp/session-handoff-test/sess-1.jsonl'
const OK = (stdout: string) => ({
  value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
const isAdvisor = (e: unknown) => /compact_advisor\.py$/.test((e as { argv: readonly string[] }).argv[1] ?? '')
const band = (surface: 'terminal' | 'desktop') =>
  ({
    plugin: PLUGIN,
    surface,
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80 },
  }) as never

// The test stands in for the engine: classic events and ids need an answer.
let sessionId = 'sess-1'
const bottom = (on: On, onStop: (e: Record<string, unknown>) => void = () => {}) => {
  sessionId = 'sess-1'
  on('classic.UserPromptSubmit', async () => ({}))
  on('classic.Stop', async (_$, e) => {
    onStop(e as Record<string, unknown>)
    return {}
  })
  on('prompt.submit', async (_$, e) => ({ ...(e as object) }) as never)
  on('session.end', async () => ({}) as never)
  on('ui.render', async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text>engine band</Text>
      </Box>
    )
  })
  on('session.id', async () => ({ value: sessionId }))
  on('session.cwd', async () => ({ value: '/work' }))
}

const stopPayload = { session_id: 'sess-1', transcript_path: TRANSCRIPT, cwd: '/work', stop_hook_active: false } as never

describe('session-handoff mod', () => {
  test('opt-in unset: nothing spawned, Stop payload goes down unmarked, nothing drawn', async ($, on) => {
    const seen: Record<string, unknown>[] = []
    bottom(on, e => seen.push(e))
    mock.env(on, {})
    const calls: unknown[] = []
    on('process.run', async (_$, e) => {
      if (isAdvisor(e)) calls.push(e)
      return OK('{"systemMessage":"x"}')
    })
    await $.classic.Stop(stopPayload)
    expect(calls.length).toBe(0)
    expect(seen.length).toBe(1)
    expect(seen[0]?.['session_handoff_mod']).toBeUndefined()
    const ui = await $.ui.mount(band('terminal'))
    expect(await ui.find({ type: 'Text', text: /engine band/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /checkpoint/ })).toBeUndefined()
  })

  test('opt-in set: spawns the advisor, marks Stop, draws the hint above the engine band', async ($, on) => {
    const seen: Record<string, unknown>[] = []
    bottom(on, e => seen.push(e))
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    const calls: { argv: readonly string[]; init?: { stdin?: string; env?: Record<string, string>; timeoutMs?: number } }[] = []
    on('process.run', async (_$, e) => {
      if (isAdvisor(e)) calls.push(e as never)
      return OK('{"systemMessage":"looks like a natural checkpoint"}')
    })
    const toasts = record(on, 'ui.toast')
    // An upstream payload that already carries the marker must not no-op the mod's own run.
    await $.classic.Stop({ ...(stopPayload as object), session_handoff_mod: true } as never)

    expect(calls.length).toBe(1)
    expect(calls[0]?.argv[0]).toBe('python3')
    expect(calls[0]?.argv[1]).toMatch(/server\/compact_advisor\.py$/)
    expect(calls[0]?.init?.timeoutMs).toBe(5000)
    expect(calls[0]?.init?.env).toEqual({ CLAUDE_CODE_SESSION_ID: 'sess-1' })
    const stdin = JSON.parse(calls[0]?.init?.stdin ?? '{}')
    expect(stdin.session_id).toBe('sess-1')
    expect(stdin.transcript_path).toBe(TRANSCRIPT)
    expect('session_handoff_mod' in stdin).toBe(false)
    expect(seen[0]?.['session_handoff_mod']).toBe(true)
    expect(toasts).toEqual(['looks like a natural checkpoint'])

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount(band(surface))
      expect(await ui.find({ type: 'Text', text: /natural checkpoint/ })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /engine band/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('advisor spawn refused or failing: Stop goes down unmarked, no hint', async ($, on) => {
    const seen: Record<string, unknown>[] = []
    bottom(on, e => seen.push(e))
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    on('process.run', async () => ({ deny: 'sandbox says no' }))
    await $.classic.Stop(stopPayload)
    expect(seen.length).toBe(1)
    expect(seen[0]?.['session_handoff_mod']).toBeUndefined()
    const ui = await $.ui.mount(band('terminal'))
    expect(await ui.find({ type: 'Button', key: 'dismiss' })).toBeUndefined()
  })

  test('advisor timeout: it ran, so Stop is marked and no hint is drawn', async ($, on) => {
    const seen: Record<string, unknown>[] = []
    bottom(on, e => seen.push(e))
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    on('process.run', async () => ({ deny: '$.process.run(python3) aborted: still running after 5000ms' }))
    await $.classic.Stop(stopPayload)
    expect(seen[0]?.['session_handoff_mod']).toBe(true)
  })

  test('advice is not published when the session changed during the spawn', async ($, on) => {
    bottom(on)
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    on('process.run', async () => {
      sessionId = 'sess-2'
      return OK('{"systemMessage":"stale hint"}')
    })
    const toasts = record(on, 'ui.toast')
    await $.classic.Stop(stopPayload)
    expect(toasts).toEqual([])
    const ui = await $.ui.mount(band('terminal'))
    expect(await ui.find({ type: 'Button', key: 'dismiss' })).toBeUndefined()
  })

  test('advice is not published over a reset that happened during the spawn', async ($, on) => {
    bottom(on)
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    let runs = 0
    on('process.run', async () => {
      runs += 1
      // The second spawn is overtaken by the user's next prompt, which clears advice.
      if (runs === 2) await $.prompt.submit({ text: 'next' } as never)
      return OK(`{"systemMessage":"hint ${runs}"}`)
    })
    const toasts = record(on, 'ui.toast')
    await $.classic.Stop(stopPayload)
    await $.classic.Stop(stopPayload)
    expect(toasts).toEqual(['hint 1'])
    const ui = await $.ui.mount(band('terminal'))
    expect(await ui.find({ type: 'Button', key: 'dismiss' })).toBeUndefined()
  })

  test('empty advice draws nothing', async ($, on) => {
    bottom(on)
    mock.env(on, { SESSION_HANDOFF_COMPACT_HINT: 'typesafe' })
    on('process.run', async () => OK('{}'))
    await $.classic.Stop(stopPayload)
    const ui = await $.ui.mount(band('terminal'))
    expect(await ui.find({ type: 'Text', text: /engine band/ })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'dismiss' })).toBeUndefined()
  })

  // Lifecycle scripts: the mod spawns each with the ORIGINAL payload, marks the
  // payload it passes down, and merges the script's output.
  const spawns = (on: On, stdout: (script: string) => string, stderr = '') => {
    const seen: { script: string; stdin: Record<string, unknown>; timeoutMs?: number }[] = []
    on('process.run', async (_$, e) => {
      const { argv, init } = e as never as { argv: string[]; init: { stdin: string; timeoutMs?: number } }
      const script = (argv[1] ?? '').split('/').slice(-2).join('/')
      seen.push({ script, stdin: JSON.parse(init.stdin), timeoutMs: init.timeoutMs })
      return {
        value: { exitCode: 0, stdout: stdout(script), stderr, isStdoutTruncated: false, isStderrTruncated: false },
      }
    })
    return seen
  }
  const record = (on: On, event: 'ui.toast' | 'ui.log') => {
    const lines: string[] = []
    const note = (e: { text: string }) => void lines.push(e.text)
    if (event === 'ui.toast') on('ui.toast', async (_$, e, next) => (note(e), next(e)))
    else on('ui.log', async (_$, e, next) => (note(e), next(e)))
    return lines
  }
  const bottomClassic = (on: On, event: 'SessionStart' | 'PreCompact' | 'UserPromptSubmit', result: object = {}) => {
    const seen: Record<string, unknown>[] = []
    const bottom = async (_$: unknown, e: unknown) => {
      seen.push(e as Record<string, unknown>)
      return result as never
    }
    if (event === 'SessionStart') on('classic.SessionStart', bottom)
    else if (event === 'PreCompact') on('classic.PreCompact', bottom)
    else on('classic.UserPromptSubmit', bottom)
    on('session.cwd', async () => ({ value: '/work' }))
    return seen
  }

  test('classic.SessionStart: spawns session-start.py, marks, merges context and message', async ($, on) => {
    const down = bottomClassic(on, 'SessionStart', { additionalContext: ['other plugin'] })
    const out = JSON.stringify({
      hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: 'recovery checkpoint' },
      systemMessage: 'launcher notice',
    })
    const ran = spawns(on, () => out)
    const logs = record(on, 'ui.log')
    const result = await $.classic.SessionStart({ source: 'compact', cwd: '/work' } as never)
    expect(down[0]?.['session_handoff_mod']).toBe(true)
    expect(ran.length).toBe(1)
    expect(ran[0]?.script).toBe('hooks/session-start.py')
    expect(ran[0]?.stdin).toMatchObject({ source: 'compact', cwd: '/work', hook_event_name: 'SessionStart' })
    expect('session_handoff_mod' in (ran[0]?.stdin ?? {})).toBe(false)
    expect(ran[0]?.timeoutMs).toBe(5000)
    expect(result.additionalContext).toEqual(['other plugin', 'recovery checkpoint'])
    expect(logs).toEqual(['launcher notice'])
  })

  test('classic.SessionStart: fork is outside the classic matcher, nothing spawned', async ($, on) => {
    const down = bottomClassic(on, 'SessionStart')
    const ran = spawns(on, () => '{}')
    await $.classic.SessionStart({ source: 'fork' } as never)
    expect(ran.length).toBe(0)
    expect(down[0]?.['session_handoff_mod']).toBeUndefined()
  })

  test('classic.SessionStart: garbage output or a failed spawn fails open', async ($, on) => {
    bottomClassic(on, 'SessionStart', { additionalContext: ['kept'] })
    spawns(on, () => 'not json')
    const logs = record(on, 'ui.log')
    const result = await $.classic.SessionStart({ source: 'startup' } as never)
    expect(result.additionalContext).toEqual(['kept'])
    expect(logs).toEqual([])
  })

  test('classic.SessionStart: a rejecting process.run still lets the session start', async ($, on) => {
    const down = bottomClassic(on, 'SessionStart')
    on('process.run', async () => ({ deny: 'sandbox says no' }))
    const result = await $.classic.SessionStart({ source: 'startup' } as never)
    expect(result.additionalContext).toBeUndefined()
    expect(down.length).toBe(1)
    // Nothing ran: the classic hook must not be silenced.
    expect(down[0]?.['session_handoff_mod']).toBeUndefined()
  })

  test('classic.UserPromptSubmit: spawns the consent script with the original payload and marks', async ($, on) => {
    const down = bottomClassic(on, 'UserPromptSubmit')
    const ran = spawns(on, () => '{}')
    await $.classic.UserPromptSubmit({ prompt: 'session-handoff telemetry yes', cwd: '/work' } as never)
    expect(ran.length).toBe(1)
    expect(ran[0]?.script).toBe('hooks/user-prompt-submit.py')
    expect(ran[0]?.stdin).toMatchObject({ prompt: 'session-handoff telemetry yes' })
    expect('session_handoff_mod' in (ran[0]?.stdin ?? {})).toBe(false)
    expect(ran[0]?.timeoutMs).toBe(1000)
    expect(down[0]?.['session_handoff_mod']).toBe(true)
    expect(down[0]?.['prompt']).toBe('session-handoff telemetry yes')
  })

  test('classic.UserPromptSubmit: a rejecting process.run never blocks the prompt', async ($, on) => {
    const down = bottomClassic(on, 'UserPromptSubmit', { additionalContext: ['kept'] })
    on('process.run', async () => ({ deny: 'nope' }))
    const result = await $.classic.UserPromptSubmit({ prompt: 'hi' } as never)
    expect(result).toEqual({ additionalContext: ['kept'] })
    expect(down.length).toBe(1)
    expect(down[0]?.['session_handoff_mod']).toBeUndefined()
  })

  test('classic.PreCompact: spawns checkpoint.py, marks, toasts on success', async ($, on) => {
    const down = bottomClassic(on, 'PreCompact')
    const ran = spawns(on, () => '{}')
    const toasts = record(on, 'ui.toast')
    await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null, cwd: '/work' } as never)
    expect(ran.length).toBe(1)
    expect(ran[0]?.script).toBe('server/checkpoint.py')
    expect(ran[0]?.stdin).toMatchObject({ trigger: 'auto', hook_event_name: 'PreCompact' })
    expect('session_handoff_mod' in (ran[0]?.stdin ?? {})).toBe(false)
    expect(ran[0]?.timeoutMs).toBe(5000)
    expect(down[0]?.['session_handoff_mod']).toBe(true)
    expect(toasts).toEqual(['session-handoff: checkpoint saved'])
  })

  test('classic.PreCompact: no toast when the script reports a skip on stderr', async ($, on) => {
    bottomClassic(on, 'PreCompact')
    spawns(on, () => '{}', 'session-handoff checkpoint skipped: cwd')
    const toasts = record(on, 'ui.toast')
    await $.classic.PreCompact({ trigger: 'manual', custom_instructions: null } as never)
    expect(toasts).toEqual([])
  })

  test('classic.PreCompact: a rejecting process.run never blocks compaction', async ($, on) => {
    const down = bottomClassic(on, 'PreCompact')
    on('process.run', async () => ({ deny: 'nope' }))
    const toasts = record(on, 'ui.toast')
    await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null } as never)
    expect(down.length).toBe(1)
    expect(down[0]?.['session_handoff_mod']).toBeUndefined()
    expect(toasts).toEqual([])
  })

  test('a spawn that cannot start (ENOENT) leaves the payload unmarked', async ($, on) => {
    const down = bottomClassic(on, 'UserPromptSubmit')
    on('process.run', async () => ({ deny: '$.process.run(python3) failed to start: ENOENT' }))
    await $.classic.UserPromptSubmit({ prompt: 'hi' } as never)
    expect(down[0]?.['session_handoff_mod']).toBeUndefined()
  })

  for (const event of ['SessionStart', 'UserPromptSubmit', 'PreCompact'] as const) {
    test(`classic.${event}: a timed-out script ran, so the payload is marked (no toast)`, async ($, on) => {
      const down = bottomClassic(on, event)
      on('process.run', async () => ({ deny: '$.process.run(python3) aborted: still running after 1000ms' }))
      const toasts = record(on, 'ui.toast')
      if (event === 'SessionStart') await $.classic.SessionStart({ source: 'startup' } as never)
      else if (event === 'UserPromptSubmit') await $.classic.UserPromptSubmit({ prompt: 'hi' } as never)
      else await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null } as never)
      expect(down[0]?.['session_handoff_mod']).toBe(true)
      expect(toasts).toEqual([])
    })
  }

  test('a non-zero exit still counts as ran and marks the payload', async ($, on) => {
    const down = bottomClassic(on, 'PreCompact')
    on('process.run', async () => ({
      value: { exitCode: 2, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false },
    }))
    const toasts = record(on, 'ui.toast')
    await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null } as never)
    expect(down[0]?.['session_handoff_mod']).toBe(true)
    expect(toasts).toEqual([])
  })

  test('scripts get CLAUDE_CODE_SESSION_ID and never the marker on stdin', async ($, on) => {
    bottomClassic(on, 'SessionStart')
    const envs: (Record<string, string> | undefined)[] = []
    const stdins: Record<string, unknown>[] = []
    on('process.run', async (_$, e) => {
      const { init } = e as never as { init: { env?: Record<string, string>; stdin: string } }
      envs.push(init.env)
      stdins.push(JSON.parse(init.stdin))
      return OK('{}')
    })
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-9', session_handoff_mod: true } as never)
    expect(envs[0]).toEqual({ CLAUDE_CODE_SESSION_ID: 'sess-9' })
    expect('session_handoff_mod' in (stdins[0] ?? {})).toBe(false)
  })
})
