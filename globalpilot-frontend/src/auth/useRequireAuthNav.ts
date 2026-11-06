import { useCallback } from 'react'
import { useNavigate } from 'react-router'
import { useAuthStore } from '../store/authStore'

/** Navigate to a protected path, or send the user to login first. */
export function useRequireAuthNav() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const token = useAuthStore((s) => s.token)
  const navigate = useNavigate()

  return useCallback(
    (path: string) => {
      if (isAuthenticated || token) {
        navigate(path)
        return
      }
      navigate('/login', { state: { from: path } })
    },
    [isAuthenticated, token, navigate],
  )
}
