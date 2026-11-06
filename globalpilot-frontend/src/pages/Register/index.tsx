import { useMemo, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router'
import { useAuth } from '../../store/authStore'

type AuthLocationState = {
  from?: string
}

const Register = () => {
  const { registerWithEmail, isAuthenticated, ready } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const redirectTo = useMemo(() => {
    const state = location.state as AuthLocationState | null
    return state?.from || '/research'
  }, [location.state])

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  if (!ready) {
    return <main className="gp-login" />
  }

  if (isAuthenticated) {
    return <Navigate to={redirectTo} replace />
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setPending(true)
    try {
      await registerWithEmail(email, password)
      navigate(redirectTo, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to create account.')
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

        <h1 className="gp-login__title">Create your account</h1>
        <p className="gp-login__lede">
          Register with email and password to save research and workspace context.
        </p>

        <form className="gp-login__form gp-login__form--solo" onSubmit={onSubmit} noValidate>
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
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 6 characters"
              required
              minLength={6}
            />
          </label>

          <label className="gp-login__field">
            <span>Confirm password</span>
            <input
              type="password"
              name="confirmPassword"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Repeat password"
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
            {pending ? 'Creating account…' : 'Create account'}
          </button>
        </form>

        <p className="gp-login__foot">
          Already have an account?{' '}
          <Link to="/login" state={{ from: redirectTo }}>
            Sign in
          </Link>
          <span className="gp-login__foot-sep">·</span>
          <Link to="/">Back to brand center</Link>
        </p>
      </div>
    </main>
  )
}

export default Register
