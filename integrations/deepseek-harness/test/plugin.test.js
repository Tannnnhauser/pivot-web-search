import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { apply, resolveConfig } from '../src/index.js'
import { parseSearchResponse } from '../src/protocol.js'

function subprocessReturning(result) {
  const calls = []
  return {
    calls,
    async resolveExecutable(command) {
      return `/resolved/${command}`
    },
    spawn(spec) {
      calls.push(spec)
      return {
        done: Promise.resolve({ exitCode: 0, signal: null }),
        async waitForExit() {
          return true
        },
        collected: {
          stdout: {
            readFrom() {
              return { text: JSON.stringify(result), lossy: false, nextOffset: 1 }
            },
          },
        },
      }
    },
  }
}

test('bundle declares an installable DSH patch without DSH source paths', async () => {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  const patch = await readFile(new URL('../cordis.patch.yml', import.meta.url), 'utf8')
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml')
  assert.match(patch, /name: pivot-web-search-dsh/)
  assert.match(patch, /- id: tool-web\s+disabled: false/)
  assert.doesNotMatch(patch, /packages\/web/)
})

test('plugin registers standard search and fetch providers', async () => {
  const searchResult = {
    protocolVersion: 1,
    ok: true,
    result: {
      content: 'answer',
      sources: [{ url: 'https://example.com', title: 'Example' }],
      truncated: false,
    },
  }
  const subprocess = subprocessReturning(searchResult)
  let searchProvider
  let fetchProvider
  const ctx = {
    subprocess,
    web: {
      registerSearchProvider(provider) {
        searchProvider = provider
      },
      registerFetchProvider(provider) {
        fetchProvider = provider
      },
    },
  }

  apply(ctx, { mode: 'super', env: { TAVILY_API_KEY: 'forwarded' } })
  assert.equal(searchProvider.id, 'pivot')
  assert.equal(fetchProvider.id, 'pivot')
  const result = await searchProvider.search({ query: 'q', maxResults: 7 })
  assert.equal(result.content, 'answer')
  const request = JSON.parse(subprocess.calls[0].stdio.stdin.data)
  assert.deepEqual(request, {
    protocolVersion: 1,
    method: 'search',
    params: { query: 'q', maxResults: 7, mode: 'super' },
  })
  assert.deepEqual(subprocess.calls[0].env, { TAVILY_API_KEY: 'forwarded' })
})

test('configuration rejects invalid modes and limits', () => {
  assert.throws(() => resolveConfig({ mode: 'auto' }), /mode/)
  assert.throws(() => resolveConfig({ stdoutMaxBytes: 0 }), /stdoutMaxBytes/)
})

test('provider cleans up a failed bridge process and returns a stable error', async () => {
  let searchProvider
  let terminateCalls = 0
  let waitCalls = 0
  const ctx = {
    subprocess: {
      async resolveExecutable(command) {
        return `/resolved/${command}`
      },
      spawn() {
        return {
          done: Promise.reject(new Error('spawn failed after handle creation')),
          terminate() {
            terminateCalls += 1
          },
          async waitForExit() {
            waitCalls += 1
            return true
          },
          collected: {},
        }
      },
    },
    web: {
      registerSearchProvider(provider) {
        searchProvider = provider
      },
      registerFetchProvider() {},
    },
  }

  apply(ctx)
  await assert.rejects(
    searchProvider.search({ query: 'q', maxResults: 1 }),
    error => error.code === 'WEB_PROVIDER_ERROR' && error.message === 'Pivot provider failed',
  )
  assert.equal(terminateCalls, 1)
  assert.equal(waitCalls, 1)
})

test('protocol parser rejects extra and unsafe source fields', () => {
  const response = JSON.stringify({
    protocolVersion: 1,
    ok: true,
    result: {
      sources: [{ url: 'file:///etc/passwd' }],
      truncated: false,
    },
  })
  assert.throws(() => parseSearchResponse(response), /source URL/)
})
