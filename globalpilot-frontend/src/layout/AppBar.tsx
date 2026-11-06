import { useEffect, useState } from 'react'
import { NavLink, Link, useLocation, useNavigate } from 'react-router'
import { useAuth } from '../store/authStore'
import { useRequireAuthNav } from '../auth/useRequireAuthNav'

const NAV = [
  { to: '/', label: 'Home', end: true },
  { to: '/workbench', label: '工作台', requiresAuth: true },
  { to: '/research', label: 'Research', requiresAuth: true },
  { to: '/marketing', label: 'Marketing', requiresAuth: true },
  { to: '/eval', label: 'Eval', requiresAuth: true },
  { to: '/assets', label: 'Assets' },
] as const

const AppBar = () => {
  const [scrolled, setScrolled] = useState(false)
  const [open, setOpen] = useState(false)
  const { user, isAuthenticated } = useAuth()
  const goProtected = useRequireAuthNav()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const onNavClick = (item: (typeof NAV)[number]) => {
    setOpen(false)
    if ('requiresAuth' in item && item.requiresAuth) {
      goProtected(item.to)
      return
    }
    navigate(item.to)
  }

  const initial = user?.name?.charAt(0)?.toUpperCase() || 'G'

  return (
    <header className={`gp-appbar${scrolled ? ' is-scrolled' : ''}`}>
      <div className="gp-appbar__inner">
        <Link to="/" className="gp-appbar__brand" onClick={() => setOpen(false)}>
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

        <nav className="gp-appbar__nav" aria-label="Primary">
          {NAV.map((item) =>
            'requiresAuth' in item && item.requiresAuth ? (
              <button
                key={item.to}
                type="button"
                className={`gp-appbar__link${location.pathname === item.to ? ' is-active' : ''}`}
                onClick={() => onNavClick(item)}
              >
                {item.label}
              </button>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={'end' in item ? item.end : false}
                className={({ isActive }) =>
                  `gp-appbar__link${isActive ? ' is-active' : ''}`
                }
              >
                {item.label}
              </NavLink>
            ),
          )}
        </nav>

        <div className="flex items-center gap-2">
          <button
            type="button"
            className="gp-btn gp-btn--primary gp-appbar__cta"
            onClick={() => {
              setOpen(false)
              goProtected('/research')
            }}
          >
            Start research
          </button>

          {isAuthenticated ? (
            <NavLink
              to="/settings"
              className={({ isActive }) =>
                `gp-appbar__avatar${isActive ? ' is-active' : ''}`
              }
              aria-label="Open profile"
              title={user?.email}
              onClick={() => setOpen(false)}
            >
              <span className="text-sm font-semibold leading-none">{initial}</span>
            </NavLink>
          ) : location.pathname !== '/login' ? (
            <Link
              to="/login"
              className="gp-btn gp-btn--ghost gp-btn--sm"
              onClick={() => setOpen(false)}
            >
              Sign in
            </Link>
          ) : null}

          <button
            type="button"
            className="gp-appbar__menu"
            aria-expanded={open}
            aria-controls="gp-mobile-nav"
            aria-label={open ? 'Close menu' : 'Open menu'}
            onClick={() => setOpen((v) => !v)}
          >
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden>
              {open ? (
                <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.6" />
              ) : (
                <path d="M3 5h12M3 9h12M3 13h12" stroke="currentColor" strokeWidth="1.6" />
              )}
            </svg>
          </button>
        </div>
      </div>

      {open ? (
        <nav id="gp-mobile-nav" className="gp-appbar__drawer" aria-label="Mobile">
          {NAV.map((item) =>
            'requiresAuth' in item && item.requiresAuth ? (
              <button
                key={item.to}
                type="button"
                className={`gp-appbar__link text-left${location.pathname === item.to ? ' is-active' : ''}`}
                onClick={() => onNavClick(item)}
              >
                {item.label}
              </button>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={'end' in item ? item.end : false}
                className={({ isActive }) =>
                  `gp-appbar__link${isActive ? ' is-active' : ''}`
                }
                onClick={() => setOpen(false)}
              >
                {item.label}
              </NavLink>
            ),
          )}
          {isAuthenticated ? (
            <NavLink
              to="/settings"
              className={({ isActive }) =>
                `gp-appbar__link${isActive ? ' is-active' : ''}`
              }
              onClick={() => setOpen(false)}
            >
              Profile
            </NavLink>
          ) : (
            <NavLink
              to="/login"
              className={({ isActive }) =>
                `gp-appbar__link${isActive ? ' is-active' : ''}`
              }
              onClick={() => setOpen(false)}
            >
              Sign in
            </NavLink>
          )}
          <button
            type="button"
            className="gp-btn gp-btn--primary mt-2"
            onClick={() => {
              setOpen(false)
              goProtected('/research')
            }}
          >
            Start research
          </button>
        </nav>
      ) : null}
    </header>
  )
}

export default AppBar
