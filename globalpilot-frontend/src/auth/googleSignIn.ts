export const GOOGLE_CLIENT_ID =
  '884569604770-nnsae7rk66rttqutmebfdq0o534sml4l.apps.googleusercontent.com'

type MountGoogleButtonOptions = {
  onCredential: (credential: string) => void
  onMissingCredential: () => void
  onUnavailable: () => void
}

export function mountGoogleSignInButton(
  el: HTMLElement,
  options: MountGoogleButtonOptions,
): () => void {
  let cancelled = false
  let timer = 0

  const mount = () => {
    if (cancelled) return true
    const google = window.google
    if (!google?.accounts?.id) return false

    google.accounts.id.initialize({
      client_id: GOOGLE_CLIENT_ID,
      ux_mode: 'popup',
      callback: (res) => {
        if (!res.credential) {
          options.onMissingCredential()
          return
        }
        options.onCredential(res.credential)
      },
    })

    const width = Math.min(
      400,
      Math.max(240, Math.floor(el.getBoundingClientRect().width) || 320),
    )
    google.accounts.id.renderButton(el, {
      type: 'standard',
      theme: 'outline',
      size: 'large',
      text: 'continue_with',
      shape: 'rectangular',
      width,
    })
    return true
  }

  if (!mount()) {
    let tries = 0
    timer = window.setInterval(() => {
      tries += 1
      if (mount() || tries > 50) {
        window.clearInterval(timer)
        if (!cancelled && tries > 50 && !window.google?.accounts?.id) {
          options.onUnavailable()
        }
      }
    }, 100)
  }

  return () => {
    cancelled = true
    if (timer) window.clearInterval(timer)
    el.replaceChildren()
  }
}
