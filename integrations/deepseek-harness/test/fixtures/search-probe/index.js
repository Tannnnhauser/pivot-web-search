import { writeFile } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'

export const name = 'pivot-web-search-dsh-search-probe'
export const inject = ['web']

export async function apply(ctx, config) {
  if (typeof config.outputPath !== 'string' || config.outputPath.length === 0) {
    throw new TypeError('search probe outputPath must be non-empty')
  }
  if (typeof config.query !== 'string' || config.query.length === 0) {
    throw new TypeError('search probe query must be non-empty')
  }

  let failure
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await ctx.web.search({ query: config.query, maxResults: 3 })
      if (Array.isArray(result.sources) && result.sources.length > 0) {
        await writeFile(config.outputPath, JSON.stringify(result))
        return
      }
      failure = new Error('search returned no sources')
    } catch (error) {
      failure = error
    }
    await delay(1_000 * (attempt + 1))
  }
  throw new Error('search probe failed after three attempts', { cause: failure })
}
