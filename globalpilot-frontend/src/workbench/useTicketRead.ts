import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from '../api/http'
import { fetchTeamMembers, type TeamMemberOption } from '../api/team'
import { ticketReadErrorMessage } from '../api/ticket'
import { useAuthStore } from '../store/authStore'
import { useLiveTeamStore } from './liveTeamStore'
import { createTicketReadGuard, observeTicketReadAccessFailure, type TicketReadScope } from './ticketReading'

function currentReadScope(): TicketReadScope | null {
  const auth = useAuthStore.getState()
  const team = useLiveTeamStore.getState()
  const membership = team.membership
  return auth.isAuthenticated && auth.user?.id === team.userId && membership?.status === 'active' && (membership.role === 'admin' || membership.role === 'employee') && team.currentTeamId === membership.team_id
    ? { userId: auth.user!.id, teamId: membership.team_id, role: membership.role }
    : null
}

export function useTicketRead<T>({ scope, resource, view, read, validate, canRefresh }: {
  scope: TicketReadScope
  resource: string
  view: 'list' | 'detail'
  read: (signal: AbortSignal) => Promise<T>
  validate: (data: T) => boolean
  canRefresh?: () => boolean
}) {
  const userId = scope.userId
  const teamId = scope.teamId
  const role = scope.role
  const [scopeEpoch, setScopeEpoch] = useState(0)
  const guard = useMemo(() => createTicketReadGuard({ userId, teamId, role }, resource, scopeEpoch), [userId, teamId, role, resource, scopeEpoch])
  const [data, setData] = useState<T | null>(null)
  const [members, setMembers] = useState<TeamMemberOption[]>([])
  const [namesLoaded, setNamesLoaded] = useState(false)
  const [namesError, setNamesError] = useState('')
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [denialVersion, setDenialVersion] = useState(0)
  const requestVersion = useRef(0)
  const controllerRef = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    if (canRefresh && !canRefresh()) return false
    const snapshot = guard.capture()
    if (!guard.isCurrent(snapshot, currentReadScope(), resource)) return false
    const version = ++requestVersion.current
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const matches = () => version === requestVersion.current && guard.isCurrent(snapshot, currentReadScope(), resource)
    setData(null)
    setMembers([])
    setNamesLoaded(false)
    setNamesError('')
    setError('')
    setStatus('loading')
    // The GET may still succeed after an admin is downgraded. Re-read the
    // membership before fetching so the page's role scope stays authoritative.
    await useLiveTeamStore.getState().refreshTeams(userId, true)
    if (!matches()) return false
    const membershipFailure = (failure: unknown) => failure instanceof ApiError && (failure.status === 401 || failure.status === 403 || failure.code === 'ACCOUNT_DISABLED')
    let accessDenial: unknown = null
    const observeDenial = <R,>(request: Promise<R>) => observeTicketReadAccessFailure(request, (failure) => {
      if (matches() && accessDenial === null) {
        accessDenial = failure
        // Revoke the operation draft immediately, even if another read in the
        // same group is still waiting. Abort is only for these GET requests.
        setDenialVersion((value) => value + 1)
        controller.abort()
      }
    })
    const [primary, people] = await Promise.allSettled([observeDenial(read(controller.signal)), observeDenial(fetchTeamMembers(teamId, controller.signal))])
    if (!matches()) return false
    const failed = accessDenial ?? (primary.status === 'rejected' ? primary.reason : null)
    if (failed) {
      setError(ticketReadErrorMessage(failed, view))
      setStatus('error')
      if (membershipFailure(failed) || (primary.status === 'fulfilled' && people.status === 'rejected')) await useLiveTeamStore.getState().refreshTeams(userId, true)
      return false
    }
    if (primary.status !== 'fulfilled' || !validate(primary.value)) {
      setError('返回的工单信息与当前团队不一致，请刷新后重试。')
      setStatus('error')
      return false
    }
    setData(primary.value)
    setRevision((value) => value + 1)
    setStatus('ready')
    if (people.status === 'fulfilled') {
      setMembers(people.value)
      setNamesLoaded(true)
    } else {
      setNamesError('成员姓名暂时无法读取，当前显示成员标识。可刷新后重试。')
    }
    return true
  }, [guard, resource, read, validate, userId, teamId, view, canRefresh])

  const clearData = useCallback(() => {
    ++requestVersion.current
    controllerRef.current?.abort()
    setData(null)
    setMembers([])
    setNamesLoaded(false)
    setNamesError('')
    setStatus('loading')
  }, [])

  useEffect(() => {
    guard.open()
    let invalidated = false
    const invalidate = () => {
      if (invalidated || guard.isCurrent(guard.capture(), currentReadScope(), resource)) return
      invalidated = true
      guard.close()
      // A role A→B→A change may be batched into one React render, keeping the
      // outer key unchanged. Start a fresh guard even in that case.
      setScopeEpoch((value) => value + 1)
      controllerRef.current?.abort()
      setData(null)
      setMembers([])
      setNamesLoaded(false)
      setNamesError('')
      setError('')
      setStatus('loading')
    }
    const unsubscribeAuth = useAuthStore.subscribe(invalidate)
    const unsubscribeTeam = useLiveTeamStore.subscribe(invalidate)
    const refreshVisible = () => { if (document.visibilityState !== 'hidden') void refresh() }
    void Promise.resolve().then(refresh)
    window.addEventListener('focus', refreshVisible)
    document.addEventListener('visibilitychange', refreshVisible)
    return () => {
      guard.close()
      controllerRef.current?.abort()
      unsubscribeAuth()
      unsubscribeTeam()
      window.removeEventListener('focus', refreshVisible)
      document.removeEventListener('visibilitychange', refreshVisible)
    }
  }, [guard, resource, refresh])

  return { data, members, namesLoaded, namesError, status, error, refresh, clearData, revision, generation: `${scopeEpoch}:${denialVersion}` }
}
