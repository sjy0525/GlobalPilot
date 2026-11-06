import test from 'node:test'
import assert from 'node:assert/strict'

import {
  profileMarketingRunUrl,
  profileMarketingTeam,
  verifyProfileMarketingRuns,
} from '../src/pages/Profile/marketingRuns.ts'

const membership = {
  team_id: 'team-a', user_id: 'user-a', role: 'employee',
  status: 'active', team_name: 'Verified team',
}
const live = { userId: 'user-a', status: 'ready', membership }

test('profile marketing history requires a live membership for the current authenticated account', () => {
  assert.equal(profileMarketingTeam('user-a', live), 'team-a')
  assert.equal(profileMarketingTeam('user-a', { ...live, membership: { ...membership, role: 'admin' } }), 'team-a')
  for (const invalid of [
    { ...live, userId: 'previous-account' },
    { ...live, status: 'loading' },
    { ...live, status: 'error' },
    { ...live, membership: null },
    { ...live, membership: { ...membership, user_id: 'previous-account' } },
    { ...live, membership: { ...membership, status: 'removed' } },
    { ...live, membership: { ...membership, role: 'viewer' } },
    { ...live, membership: { ...membership, team_id: ' ' } },
  ]) {
    assert.equal(profileMarketingTeam('user-a', invalid), null)
  }
  assert.equal(profileMarketingTeam(null, live), null)
})

test('switching accounts or selecting a team cannot expose the old team before membership is verified', () => {
  assert.equal(profileMarketingTeam('user-b', live), null)
  assert.equal(profileMarketingTeam('user-a', { ...live, status: 'loading', currentTeamId: 'team-b' }), null)
  assert.equal(profileMarketingTeam('user-a', {
    ...live,
    membership: { ...membership, team_id: 'team-b' },
  }), 'team-b')
})

test('profile rejects marketing records from another owner or team instead of silently mixing them', () => {
  const owned = [{ id: 'brief-a', user_id: 'user-a', team_id: 'team-a' }]
  const before = structuredClone(owned)
  assert.equal(verifyProfileMarketingRuns(owned, 'user-a', 'team-a'), owned)
  assert.deepEqual(owned, before)
  assert.throws(() => verifyProfileMarketingRuns([
    ...owned, { id: 'brief-b', user_id: 'user-b', team_id: 'team-a' },
  ], 'user-a', 'team-a'), /account and team/)
  assert.throws(() => verifyProfileMarketingRuns([
    { id: 'brief-a', user_id: 'user-a', team_id: 'team-b' },
  ], 'user-a', 'team-a'), /account and team/)
})

test('opening a historical run explicitly identifies the clicked brief and team in the URL', () => {
  const url = new URL(profileMarketingRunUrl('clicked brief / 3', 'team & a'), 'https://test.invalid')
  assert.equal(url.pathname, '/marketing')
  assert.equal(url.searchParams.get('brief_id'), 'clicked brief / 3')
  assert.equal(url.searchParams.get('team_id'), 'team & a')
  assert.equal([...url.searchParams].length, 2)
})

test('a historical-run URL cannot omit its brief or team scope', () => {
  for (const args of [['', 'team-a'], [' ', 'team-a'], ['brief-a', ''], ['brief-a', ' ']]) {
    assert.throws(() => profileMarketingRunUrl(...args), /missing/)
  }
})
