import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router'
import { mountGoogleSignInButton } from '../../auth/googleSignIn'
import { useAuth } from '../../store/authStore'

type AuthLocationState = {
  from?: string
}

const Login = () => {
  const { loginWithEmail, loginWithGoogle, isAuthenticated, ready } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const redirectTo = useMemo(() => {
    const state = location.state as AuthLocationState | null
    return state?.from || '/workbench'
  }, [location.state])

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const googleBtnRef = useRef<HTMLDivElement>(null)
  const loginWithGoogleRef = useRef(loginWithGoogle)
  const redirectToRef = useRef(redirectTo)

  useEffect(() => {
    loginWithGoogleRef.current = loginWithGoogle
    redirectToRef.current = redirectTo
  }, [loginWithGoogle, redirectTo])

  useEffect(() => {
    if (!ready || isAuthenticated) return
    const el = googleBtnRef.current
    if (!el) return

    return mountGoogleSignInButton(el, {
      onCredential: (credential) => {
        void (async () => {
          setPending(true)
          setError(null)
          try {
            await loginWithGoogleRef.current(credential)
            navigate(redirectToRef.current, { replace: true })
          } catch (err) {
            setError(
              err instanceof Error ? err.message : 'Google sign-in failed.',
            )
          } finally {
            setPending(false)
          }
        })()
      },
      onMissingCredential: () => {
        setError('Google sign-in was cancelled or blocked. Try again.')
      },
      onUnavailable: () => {
        setError('Google script not loaded yet. Refresh and try again.')
      },
    })
  }, [navigate, ready, isAuthenticated])

  if (!ready) {
    return <main className="gp-login" />
  }

  if (isAuthenticated) {
    return <Navigate to={redirectTo} replace />
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setPending(true)
    try {
      await loginWithEmail(email, password)
      navigate(redirectTo, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to sign in.')
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="gp-login">
      <div className="gp-login__panel gp-animate-fade-up">
        <Link to="/" className="gp-login__brand">
          <span className="gp-appbar__mark" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path
                d="M7 1.5L12.5 11H1.5L7 1.5Z"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          GlobalPilot
        </Link>

        <h1 className="gp-login__title">Sign in to continue</h1>
        <p className="gp-login__lede">
          Access research, marketing workflows, and your brand workspace.
        </p>

        <div
          ref={googleBtnRef}
          className={`gp-login__google-slot${pending ? ' is-pending' : ''}`}
        />

        <div className="gp-login__divider" role="separator">
          <span>or</span>
        </div>

        <form className="gp-login__form" onSubmit={onSubmit} noValidate>
          <label className="gp-login__field">
            <span>Email</span>
            <input
              type="email"
              name="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              required
            />
          </label>

          <label className="gp-login__field">
            <span>Password</span>
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 6 characters"
              required
              minLength={6}
            />
          </label>

          {error ? <p className="gp-login__error">{error}</p> : null}

          <button
            type="submit"
            className="gp-btn gp-btn--primary gp-login__submit"
            disabled={pending}
          >
            {pending ? 'Signing in…' : 'Sign in with email'}
          </button>
        </form>

        <p className="gp-login__foot">
          New here?{' '}
          <Link to="/register" state={{ from: redirectTo }}>
            Create an account
          </Link>
          <span className="gp-login__foot-sep">·</span>
          <Link to="/">Back to brand center</Link>
        </p>
      </div>
    </main>
  )
}

export default Login
