export type WorkbenchPrincipal = {
  role?: string
  teamId?: string | null
  status?: string
}

/** Team members handle work and assigned reviews. Only admins manage members. */
export function getWorkbenchAccess(principal: WorkbenchPrincipal | null | undefined) {
  const validRole = principal?.role === 'employee' || principal?.role === 'admin'
  const validStatus = principal?.status === undefined || principal.status === 'active'
  const canAccessWorkbench = validRole && validStatus && typeof principal?.teamId === 'string' && principal.teamId.trim().length > 0
  return {
    canAccessWorkbench,
    canReview: canAccessWorkbench, // Review-desk entry only; each ticket checks its reviewer separately.
    canManageTeam: canAccessWorkbench && principal?.role === 'admin',
  }
}

export type PreviewView = 'employee' | 'admin'

export function parsePreviewView(value: string | null): PreviewView {
  return value === 'admin' ? 'admin' : 'employee'
}
