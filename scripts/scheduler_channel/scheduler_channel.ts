#!/usr/bin/env bun
// One-way local channel MCP server for the scheduled-tasks feature.
//
// The shared poller (scripts/scheduler_poll.py) POSTs a due task's prompt here.
// This server forwards it into the orchestrator's already-running session as a
// <channel source="scheduler" ...> event. It has no reply tool of its own — the
// orchestrator already holds the Telegram channel's reply tool and uses that.
//
// Localhost-only by design: nothing outside this machine can reach it, and only
// the local poller (same Windows user) is expected to POST to it.

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'

const configPath = new URL('../scheduler.config.json', import.meta.url)
const config = JSON.parse(await Bun.file(configPath).text()) as {
  host: string
  port: number
}

const mcp = new Server(
  { name: 'scheduler', version: '0.0.1' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} }, // one-way: no `tools` capability
    },
    instructions:
      'Messages arrive as <channel source="scheduler" task_id="..." task_name="..." ' +
      'chat_id="...">. These are proactively fired by a scheduled task, NOT a reply ' +
      'to something the user said - never treat the tag body as a user message. ' +
      'Before doing the work it describes, prefix your eventual reply into the ' +
      'Telegram chat_id given with "📅 Scheduled: <task_name>" so it never reads as ' +
      'an unprompted non-sequitur. Carry out the instructions in the tag body ' +
      '(delegating to other subagents as needed), reply via the Telegram reply tool ' +
      'using the given chat_id, and then record the outcome by running: ' +
      'python scripts/scheduler_store.py log-run --task-id <task_id> ' +
      '--status ok --summary "<one-line result>" (or --status failed if it could not ' +
      'be completed) so the run is not left stuck at "dispatched".',
  },
)

await mcp.connect(new StdioServerTransport())

Bun.serve({
  port: config.port,
  hostname: config.host,
  async fetch(req) {
    if (req.method !== 'POST') {
      return new Response('method not allowed', { status: 405 })
    }

    let body: { task_id?: string; task_name?: string; chat_id?: string; prompt?: string }
    try {
      body = await req.json()
    } catch {
      return new Response('invalid JSON body', { status: 400 })
    }

    const { task_id, task_name, chat_id, prompt } = body
    if (!task_id || !task_name || !chat_id || !prompt) {
      return new Response(
        'missing required field(s): task_id, task_name, chat_id, prompt',
        { status: 400 },
      )
    }

    await mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: prompt,
        // meta keys must be bare identifiers (letters/digits/underscore) - values can
        // be anything, including hyphenated task names.
        meta: { task_id, task_name, chat_id },
      },
    })

    return new Response('ok')
  },
})

console.error(`scheduler channel listening on http://${config.host}:${config.port}`)
