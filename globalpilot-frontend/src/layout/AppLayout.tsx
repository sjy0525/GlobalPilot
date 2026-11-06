import { useEffect } from 'react'
import { Outlet } from 'react-router'
import { useAuthStore } from '../store/authStore'
import AppBar from './AppBar'

const AppLayout = () => {
  useEffect(() => {
    void useAuthStore.getState().restoreSession()
  }, [])

  return (
    <div className="min-h-svh">
      <AppBar />
      <Outlet />
    </div>
  )
}

export default AppLayout
