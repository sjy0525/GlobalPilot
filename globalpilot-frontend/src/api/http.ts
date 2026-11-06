export const API_BASE = import.meta.env?.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  status: number
  code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

type AuthHooks = {
  getAccessToken: () => string | null
  refreshAccessToken: () => Promise<string | null>
  onUnauthorized: () => void
}

const AUTH_PUBLIC_PATHS = new Set([
  '/api/auth/login',
  '/api/auth/google-login',
  '/api/auth/refresh',
  '/api/auth/logout',
  '/api/register',
])

let authHooks: AuthHooks = {
  getAccessToken: () => null,
  refreshAccessToken: async () => null,
  onUnauthorized: () => {},
}

let refreshInFlight: Promise<string | null> | null = null

export function configureAuth(hooks: AuthHooks) {
  authHooks = hooks
}

function pathName(path: string) {
  return path.split('?')[0]
}

function isPublicAuthPath(path: string) {
  return AUTH_PUBLIC_PATHS.has(pathName(path))
}

function refreshOnce() {
  if (!refreshInFlight) {
    refreshInFlight = Promise.resolve(authHooks.refreshAccessToken()).finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const initialToken = authHooks.getAccessToken()
  const send = () => {
    const headers = new Headers(init?.headers)
    const token = authHooks.getAccessToken()
    if (token && !isPublicAuthPath(path)) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' })
  }

  let response = await send()
  if (response.status !== 401 || isPublicAuthPath(path)) {
    return response
  }

  // Ignore late failures from an older session (e.g. logout or account switch).
  const currentToken = authHooks.getAccessToken()
  if (initialToken && currentToken !== initialToken) {
    return currentToken ? send() : response
  }
  const refreshed = await refreshOnce()
  if (refreshed) {
    response = await send()
  }
  if (response.status === 401 && (authHooks.getAccessToken() === (refreshed ?? initialToken))) {
    authHooks.onUnauthorized()
  }
  return response
}

function readDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg)
        }
        return null
      })
      .filter((msg): msg is string => Boolean(msg))
    if (messages.length) return messages.join(' ')
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    const messages: string[] = []
    const message = typeof record.message === 'string' ? record.message.trim() : ''
    if (message) messages.push(message)
    const issues = readDetail(record.issues)
    if (issues) messages.push(issues)
    if (record.review && typeof record.review === 'object') {
      const errors = readDetail((record.review as Record<string, unknown>).errors)
      if (errors) messages.push(errors)
    }
    if (messages.length) return [...new Set(messages)].join(' ')
  }
  return null
}

export async function parseApiResponse<T>(response: Response): Promise<T> {
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? readDetail((data as { detail: unknown }).detail)
        : null
    const code =
      data && typeof data === 'object' && 'code' in data && typeof data.code === 'string'
        ? data.code
        : undefined
    throw new ApiError(detail || 'Request failed.', response.status, code)
  }
  return data as T
}

async function apiRequest(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await authorizedFetch(path, init)
  } catch (err) {
    if (err instanceof ApiError) throw err
    throw new ApiError(
      'Cannot reach the server. Make sure the API is running.',
      0,
    )
  }
}

export async function apiBlob(
  path: string,
): Promise<{ blob: Blob; filename: string; mediaType: string }> {
  const response = await apiRequest(path)
  if (!response.ok) {
    await parseApiResponse(response)
    throw new ApiError('Request failed.', response.status)
  }
  const header = response.headers.get('content-disposition')
  const star = header ? /filename\*=UTF-8''([^;]+)/i.exec(header) : null
  const basic = header ? /filename="?([^";]+)"?/i.exec(header) : null
  let filename = 'download'
  if (star?.[1]) {
    try {
      filename = decodeURIComponent(star[1])
    } catch {
      filename = star[1]
    }
  } else if (basic?.[1]) {
    filename = basic[1]
  }
  const mediaType = (response.headers.get('content-type') || '').split(';')[0].trim()
  const raw = await response.blob()
  const blob =
    mediaType && raw.type !== mediaType ? new Blob([raw], { type: mediaType }) : raw
  return { blob, filename, mediaType: blob.type || mediaType }
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiRequest(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  return parseApiResponse<T>(response)
}

export async function readNdjson(
  response: Response,
  onLine: (value: unknown) => void,
): Promise<void> {
  if (!response.body) {
    throw new ApiError('Empty stream from the server.', response.status)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed || !trimmed.startsWith('{')) continue
        let value: unknown
        try {
          value = JSON.parse(trimmed) as unknown
        } catch (error) {
          if (error instanceof SyntaxError) continue
          throw error
        }
        onLine(value)
      }
      if (done) break
    }
    const trailing = buffer.trim()
    if (trailing.startsWith('{')) {
      let value: unknown
      try {
        value = JSON.parse(trailing) as unknown
      } catch (error) {
        // Ignore a truncated frame; the caller treats a missing result as an error.
        if (error instanceof SyntaxError) return
        throw error
      }
      onLine(value)
    }
  } catch (error) {
    await reader.cancel().catch(() => {})
    throw error
  } finally {
    reader.releaseLock()
  }
}

export async function apiNdjson(
  path: string,
  init: RequestInit,
  onLine: (value: unknown) => void,
): Promise<void> {
  const response = await apiRequest(path, {
    ...init,
    headers: {
      Accept: 'application/x-ndjson',
      'Content-Type': 'application/json',
      ...(init.headers ?? {}),
    },
  })
  const contentType = response.headers.get('content-type') || ''
  if (!response.ok) {
    await parseApiResponse(response)
    return
  }
  if (contentType.includes('application/json') && !contentType.includes('ndjson')) {
    onLine(await parseApiResponse<unknown>(response))
    return
  }
  await readNdjson(response, onLine)
}

export async function apiForm<T>(
  path: string,
  form: FormData,
  method = 'POST',
): Promise<T> {
  const response = await apiRequest(path, {
    method,
    body: form,
  })
  return parseApiResponse<T>(response)
}
