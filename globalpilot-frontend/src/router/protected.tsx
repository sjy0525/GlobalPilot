import { RequireAuth } from '../auth/RequireAuth'
import Marketing from '../pages/Marketing'
import Eval from '../pages/Eval'
import Research from '../pages/Research'
import Profile from '../pages/Profile'

export function ProtectedResearch() {
  return (
    <RequireAuth>
      <Research />
    </RequireAuth>
  )
}

export function ProtectedMarketing() {
  return (
    <RequireAuth>
      <Marketing />
    </RequireAuth>
  )
}

export function ProtectedEval() {
  return (
    <RequireAuth>
      <Eval />
    </RequireAuth>
  )
}

export function ProtectedProfile() {
  return (
    <RequireAuth>
      <Profile />
    </RequireAuth>
  )
}
