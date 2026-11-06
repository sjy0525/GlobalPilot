import { Navigate, useLocation } from 'react-router'
import { useAuth } from '../store/authStore'
import type { ReactNode } from 'react'

type Props = {
  children: ReactNode
}

/** Redirects unauthenticated users to /login, preserving the intended path. */
export function RequireAuth({ children }: Props) {
  const { isAuthenticated, ready } = useAuth()
  const location = useLocation()

  if (!ready) return null

  if (!isAuthenticated) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    )
  }

  return children
}
