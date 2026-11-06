import type { BriefListItem } from '../../api/briefs'
import type { TeamMembership } from '../../api/team'

type ProfileTeamState = {
  userId: string | null
  status: string
  membership: TeamMembership | null
}

export function profileMarketingTeam(userId: string | null, live: ProfileTeamState): string | null {
  const membership = live.membership
  if (!userId || live.userId !== userId || live.status !== 'ready'
    || !membership || membership.user_id !== userId || membership.status !== 'active'
    || !['admin', 'employee'].includes(membership.role) || !membership.team_id.trim()) return null
  return membership.team_id
}

export function verifyProfileMarketingRuns(
  records: BriefListItem[],
  userId: string,
  teamId: string,
): BriefListItem[] {
  if (records.some(record => record.user_id !== userId || record.team_id !== teamId)) {
    throw new Error('Marketing runs do not match the current account and team. Please reload.')
  }
  return records
}

export function profileMarketingRunUrl(briefId: string, teamId: string): string {
  if (!briefId.trim() || !teamId.trim()) throw new Error('The marketing run or team is missing.')
  return '/marketing?' + new URLSearchParams({ brief_id: briefId, team_id: teamId }).toString()
}
