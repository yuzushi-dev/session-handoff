export type SessionHandoffAdvice = {
  /** The hint text the Python advisor printed (its `systemMessage`). */
  text: string
}

export type SessionHandoffTranscript = {
  /** Session the path belongs to; a /clear changes the id, the path with it. */
  sessionId: string
  path: string
}

declare module 'claude-code' {
  interface PluginState {
    'session-handoff': {
      advice: SessionHandoffAdvice | null
      transcript: SessionHandoffTranscript | null
    }
  }
}
