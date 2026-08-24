/** Out-of-tree DSH provider for Pivot's standard search and fetch capabilities. */

import {
  encodeFetchRequest,
  encodeSearchRequest,
  parseFetchResponse,
  parseSearchResponse,
} from './protocol.js'

export const name = 'pivot-web-search-dsh'
export const inject = ['web', 'subprocess']
export const PIVOT_PROVIDER_ID = 'pivot'

const DEFAULT_COMMAND = 'pivot-web-search-bridge'
const DEFAULT_GRACE_MS = 1000
const DEFAULT_STDOUT_MAX_BYTES = 1024 * 1024
const DEFAULT_STDERR_MAX_BYTES = 64 * 1024

export function resolveConfig(config = {}) {
  const resolved = {
    command: config.command ?? DEFAULT_COMMAND,
    args: config.args ?? [],
    cwd: config.cwd?.length ? config.cwd : process.cwd(),
    env: config.env ?? {},
    mode: config.mode ?? 'normal',
    graceMs: config.graceMs ?? DEFAULT_GRACE_MS,
    stdoutMaxBytes: config.stdoutMaxBytes ?? DEFAULT_STDOUT_MAX_BYTES,
    stderrMaxBytes: config.stderrMaxBytes ?? DEFAULT_STDERR_MAX_BYTES,
  }
  if (typeof resolved.command !== 'string' || resolved.command.length === 0) {
    throw new TypeError('pivot-web-search-dsh command must be non-empty')
  }
  if (!Array.isArray(resolved.args) || resolved.args.some(value => typeof value !== 'string')) {
    throw new TypeError('pivot-web-search-dsh args must be strings')
  }
  if (!isStringRecord(resolved.env)) throw new TypeError('pivot-web-search-dsh env must contain strings')
  if (resolved.mode !== 'normal' && resolved.mode !== 'super') {
    throw new TypeError('pivot-web-search-dsh mode must be normal or super')
  }
  for (const key of ['graceMs', 'stdoutMaxBytes', 'stderrMaxBytes']) {
    if (!Number.isSafeInteger(resolved[key]) || resolved[key] <= 0) {
      throw new TypeError(`pivot-web-search-dsh ${key} must be a positive integer`)
    }
  }
  return resolved
}

class PivotBridgeClient {
  constructor(subprocess, options) {
    this.subprocess = subprocess
    this.options = options
  }

  async search(request, signal) {
    return this.execute(encodeSearchRequest(request, this.options.mode), parseSearchResponse, signal)
  }

  async fetch(request, signal) {
    return this.execute(encodeFetchRequest(request), parseFetchResponse, signal)
  }

  async execute(stdin, parseResponse, signal) {
    if (signal?.aborted) throw operationError('Pivot operation aborted')
    let child
    try {
      const executable = await this.subprocess.resolveExecutable(this.options.command, this.options.env, signal)
      child = this.subprocess.spawn({
        argv: [executable, ...this.options.args],
        cwd: this.options.cwd,
        env: { ...this.options.env },
        graceMs: this.options.graceMs,
        signal,
        stdio: {
          stdin: { data: stdin },
          stdout: { maxBytes: this.options.stdoutMaxBytes },
          stderr: { maxBytes: this.options.stderrMaxBytes },
        },
      })
      const outcome = await child.done
      await child.waitForExit()
      if (signal?.aborted) throw operationError('Pivot operation aborted')
      const stdout = child.collected.stdout?.readFrom(0)
      if (stdout === undefined || stdout.lossy) throw operationError('Pivot bridge returned invalid output')
      const result = parseResponse(stdout.text)
      if (outcome.exitCode !== 0 || outcome.signal !== null) {
        throw operationError('Pivot bridge exited unsuccessfully')
      }
      return result
    } catch (error) {
      if (child !== undefined) {
        child.terminate()
        try {
          await child.waitForExit()
        } catch {
          // Preserve the stable provider error below; subprocess diagnostics
          // remain in the harness log rather than entering model context.
        }
      }
      if (signal?.aborted) throw operationError('Pivot operation aborted')
      if (error?.code === 'WEB_PROVIDER_ERROR') throw error
      throw operationError('Pivot provider failed', error)
    }
  }
}

export class PivotSearchProvider {
  id = PIVOT_PROVIDER_ID

  constructor(client) {
    this.client = client
  }

  available() {
    return true
  }

  search(request, signal) {
    return this.client.search(request, signal)
  }
}

export class PivotFetchProvider {
  id = PIVOT_PROVIDER_ID

  constructor(client) {
    this.client = client
  }

  available() {
    return true
  }

  fetch(request, signal) {
    return this.client.fetch(request, signal)
  }
}

export function apply(ctx, config = {}) {
  const client = new PivotBridgeClient(ctx.subprocess, resolveConfig(config))
  ctx.web.registerSearchProvider(new PivotSearchProvider(client))
  ctx.web.registerFetchProvider(new PivotFetchProvider(client))
}

function operationError(message, cause) {
  const error = new Error(message, cause === undefined ? undefined : { cause })
  error.code = 'WEB_PROVIDER_ERROR'
  return error
}

function isStringRecord(value) {
  return typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
    && Object.values(value).every(item => typeof item === 'string')
}
