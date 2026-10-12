#!/usr/bin/env node

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const integrationRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repositoryRoot = resolve(integrationRoot, '../..')
const searchProbeRoot = resolve(integrationRoot, 'test/fixtures/search-probe')
const dshPackage = process.env.PIVOT_DSH_PACKAGE ?? '@deepseek-ai/dsh@0.2.1-alpha.2'
const startupTimeoutMs = Number(process.env.PIVOT_DSH_STARTUP_TIMEOUT_MS ?? 90_000)
const searchQuery = 'Python programming language official website'

assert(Number.isSafeInteger(startupTimeoutMs) && startupTimeoutMs > 0, 'startup timeout must be a positive integer')

const dshHome = await mkdtemp(join(tmpdir(), 'pivot-dsh-profile-'))
const searchResultPath = join(dshHome, 'search-result.json')
const environment = {
  ...process.env,
  DSH_HOME: dshHome,
  DSH_TELEMETRY_DISABLED: '1',
  PIVOT_DSH_PROBE_OUTPUT: searchResultPath,
  PIVOT_DSH_PROBE_QUERY: searchQuery,
}

try {
  await verifyBridge()
  await runDsh(['plugin', '--profile', 'web', 'add', integrationRoot, searchProbeRoot])
  const config = await runDsh(['--profile', 'web', '--dump-config'])

  assert.match(config.stdout, /searchProvider: pivot/)
  assert.match(config.stdout, /fetchProvider: pivot/)
  assert.match(config.stdout, /id: tool-web/)
  assert.match(config.stdout, /id: pivot-web-search-provider/)
  assert.match(config.stdout, /name: pivot-web-search-dsh/)
  assert.match(config.stdout, /command: pivot-web-search-bridge/)

  await bootWebProfile()
  const searchResult = JSON.parse(await readFile(searchResultPath, 'utf8'))
  assert(Array.isArray(searchResult.sources) && searchResult.sources.length > 0)
  assert(searchResult.sources.every(source => /^https?:\/\//.test(source.url)))
  process.stdout.write(`DSH search smoke passed with ${dshPackage}: ${JSON.stringify(searchQuery)}\n`)
  for (const source of searchResult.sources) process.stdout.write(`- ${source.url}\n`)
} finally {
  await rm(dshHome, { recursive: true, force: true })
}

function runDsh(args, timeoutMs = 120_000) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn('pnpm', ['dlx', dshPackage, ...args], {
      cwd: repositoryRoot,
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    const timeout = setTimeout(() => {
      child.kill('SIGKILL')
      rejectPromise(new Error(`DSH command timed out: ${args.join(' ')}`))
    }, timeoutMs)

    child.stdout.on('data', chunk => {
      stdout = appendOutput(stdout, chunk)
    })
    child.stderr.on('data', chunk => {
      stderr = appendOutput(stderr, chunk)
    })
    child.once('error', error => {
      clearTimeout(timeout)
      rejectPromise(error)
    })
    child.once('close', code => {
      clearTimeout(timeout)
      if (code === 0) resolvePromise({ stdout, stderr })
      else rejectPromise(new Error(`DSH command failed (${String(code)}): ${stderr || stdout}`))
    })
  })
}

function verifyBridge() {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn('pivot-web-search-bridge', [], {
      cwd: repositoryRoot,
      env: environment,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''

    child.stdout.on('data', chunk => {
      stdout = appendOutput(stdout, chunk)
    })
    child.stderr.on('data', chunk => {
      stderr = appendOutput(stderr, chunk)
    })
    child.once('error', rejectPromise)
    child.once('close', code => {
      try {
        assert.equal(code, 2, stderr || stdout)
        const response = JSON.parse(stdout)
        assert.equal(response.error?.code, 'MALFORMED_JSON')
        resolvePromise()
      } catch (error) {
        rejectPromise(error)
      }
    })
    child.stdin.end('not-json')
  })
}

function bootWebProfile() {
  return new Promise((resolvePromise, rejectPromise) => {
    const detached = process.platform !== 'win32'
    const child = spawn('pnpm', ['dlx', dshPackage, 'web', '--no-open', '--port', '0'], {
      cwd: repositoryRoot,
      detached,
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let output = ''
    let ready = false
    let probeComplete = false
    let checkingProbe = false
    const timeout = setTimeout(() => {
      clearInterval(probePoll)
      stopChild(child, detached)
      rejectPromise(new Error(`DSH web profile did not start within ${startupTimeoutMs}ms: ${output}`))
    }, startupTimeoutMs)
    const probePoll = setInterval(async () => {
      if (!ready || probeComplete || checkingProbe) return
      checkingProbe = true
      try {
        JSON.parse(await readFile(searchResultPath, 'utf8'))
        probeComplete = true
        clearTimeout(timeout)
        clearInterval(probePoll)
        stopChild(child, detached)
      } catch {
        // The probe may not have created or finished writing the result yet.
      } finally {
        checkingProbe = false
      }
    }, 100)

    const onData = chunk => {
      output = appendOutput(output, chunk)
      if (!ready && /dsh web: http:\/\/127\.0\.0\.1:\d+\//.test(output)) {
        ready = true
      }
    }
    child.stdout.on('data', onData)
    child.stderr.on('data', onData)
    child.once('error', error => {
      clearTimeout(timeout)
      clearInterval(probePoll)
      rejectPromise(error)
    })
    child.once('close', code => {
      clearTimeout(timeout)
      clearInterval(probePoll)
      if (ready && probeComplete) resolvePromise()
      else rejectPromise(new Error(`DSH web profile exited before startup (${String(code)}): ${output}`))
    })
  })
}

function stopChild(child, detached) {
  if (child.exitCode !== null || child.signalCode !== null) return
  signalChild(child, detached, 'SIGINT')
  setTimeout(() => {
    if (child.exitCode !== null || child.signalCode !== null) return
    signalChild(child, detached, 'SIGKILL')
  }, 5_000).unref()
}

function signalChild(child, detached, signal) {
  try {
    if (detached && child.pid !== undefined) process.kill(-child.pid, signal)
    else child.kill(signal)
  } catch (error) {
    if (error?.code !== 'ESRCH') throw error
  }
}

function appendOutput(current, chunk) {
  const next = current + chunk.toString()
  return next.length > 64_000 ? next.slice(-64_000) : next
}
