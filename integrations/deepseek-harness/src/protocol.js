/** Strict Pivot Machine Protocol v1 encoder and decoder. */

export const PROTOCOL_VERSION = 1

const ERROR_CODES = new Set([
  'MALFORMED_JSON',
  'REQUEST_TOO_LARGE',
  'INVALID_REQUEST',
  'UNSUPPORTED_PROTOCOL',
  'UNSUPPORTED_METHOD',
  'SEARCH_FAILED',
  'FETCH_FAILED',
  'CANCELLED',
  'INTERNAL_ERROR',
])

export class PivotProtocolError extends Error {}

export function encodeSearchRequest(request, mode) {
  return JSON.stringify({
    protocolVersion: PROTOCOL_VERSION,
    method: 'search',
    params: {
      query: request.query,
      ...(request.maxResults === undefined ? {} : { maxResults: request.maxResults }),
      mode,
    },
  })
}

export function encodeFetchRequest(request) {
  return JSON.stringify({
    protocolVersion: PROTOCOL_VERSION,
    method: 'fetch',
    params: { url: request.url },
  })
}

export function parseSearchResponse(text) {
  return parseSearchResult(parseResponse(text))
}

export function parseFetchResponse(text) {
  return parseFetchResult(parseResponse(text))
}

function parseResponse(text) {
  let value
  try {
    value = JSON.parse(text)
  } catch (error) {
    throw new PivotProtocolError('Pivot bridge stdout is not valid JSON', { cause: error })
  }
  if (!isRecord(value) || value.protocolVersion !== PROTOCOL_VERSION || typeof value.ok !== 'boolean') {
    throw new PivotProtocolError('Pivot bridge response envelope is invalid')
  }
  if (value.ok) {
    exactKeys(value, ['protocolVersion', 'ok', 'result'])
    return value.result
  }
  exactKeys(value, ['protocolVersion', 'ok', 'error'])
  const error = parseError(value.error)
  throw new PivotProtocolError(`Pivot bridge reported ${error.code}`)
}

function parseSearchResult(value) {
  if (!isRecord(value)) throw new PivotProtocolError('Pivot search result is invalid')
  exactKeys(value, ['sources', 'truncated'], ['content'])
  if (!Array.isArray(value.sources) || typeof value.truncated !== 'boolean') {
    throw new PivotProtocolError('Pivot search result fields are invalid')
  }
  if (value.content !== undefined && typeof value.content !== 'string') {
    throw new PivotProtocolError('Pivot search content is invalid')
  }
  return {
    ...(value.content === undefined ? {} : { content: value.content }),
    sources: value.sources.map(parseSource),
    truncated: value.truncated,
  }
}

function parseFetchResult(value) {
  if (!isRecord(value)) throw new PivotProtocolError('Pivot fetch result is invalid')
  exactKeys(value, ['url', 'statusCode', 'body', 'truncated'])
  if (!isHttpUrl(value.url)) throw new PivotProtocolError('Pivot fetch URL is invalid')
  if (!Number.isSafeInteger(value.statusCode) || value.statusCode < 100 || value.statusCode > 599) {
    throw new PivotProtocolError('Pivot fetch status code is invalid')
  }
  if (!isRecord(value.body)) throw new PivotProtocolError('Pivot fetch body is invalid')
  exactKeys(value.body, ['kind', 'content'])
  if (value.body.kind !== 'text' || typeof value.body.content !== 'string') {
    throw new PivotProtocolError('Pivot fetch body fields are invalid')
  }
  if (typeof value.truncated !== 'boolean') throw new PivotProtocolError('Pivot fetch truncation is invalid')
  return value
}

function parseSource(value) {
  if (!isRecord(value)) throw new PivotProtocolError('Pivot search source is invalid')
  exactKeys(value, ['url'], ['title', 'snippet', 'publishedAt'])
  if (!isHttpUrl(value.url)) throw new PivotProtocolError('Pivot source URL is invalid')
  for (const key of ['title', 'snippet', 'publishedAt']) {
    if (value[key] !== undefined && typeof value[key] !== 'string') {
      throw new PivotProtocolError(`Pivot source ${key} is invalid`)
    }
  }
  return value
}

function parseError(value) {
  if (!isRecord(value)) throw new PivotProtocolError('Pivot bridge error is invalid')
  exactKeys(value, ['code', 'message', 'retryable'])
  if (typeof value.code !== 'string' || typeof value.message !== 'string' || typeof value.retryable !== 'boolean') {
    throw new PivotProtocolError('Pivot bridge error fields are invalid')
  }
  if (!ERROR_CODES.has(value.code)) throw new PivotProtocolError('Pivot bridge error code is unsupported')
  return value
}

function isHttpUrl(value) {
  if (typeof value !== 'string') return false
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function exactKeys(value, required, optional = []) {
  const expected = new Set([...required, ...optional])
  if (required.some(key => !(key in value)) || Object.keys(value).some(key => !expected.has(key))) {
    throw new PivotProtocolError('Pivot bridge response contains unsupported fields')
  }
}
