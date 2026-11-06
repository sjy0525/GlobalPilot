import test from 'node:test'
import assert from 'node:assert/strict'
import { getWorkbenchAccess } from '../src/workbench/permissions.ts'
import { PREVIEW_ADMIN_ID as admin, PREVIEW_EMPLOYEE_ID as employee, PREVIEW_TEAM_ID, previewAccountId } from '../src/workbench/team.ts'
import { addTeamMember, canApproveTicket, changeTeamRole, createInitialPreviewState, getUnassignedSides, isStaleTicket, reassignTicket, removeTeamMember } from '../src/workbench/model.ts'
const now = Date.parse('2026-10-24T08:00:00Z')
const fresh = () => createInitialPreviewState(now)
const first = 'GP-20261024-001'
const queue = { mode: 'queue', assigneeId: null, reviewerId: null, reason: '人员离队' }

test('only active team members with supported roles may enter the workbench', () => {
  for (const user of [null, {}, { role: 'employee' }, { role: 'admin' }, { role: 'reviewer', teamId: PREVIEW_TEAM_ID }, { role: 'employee', teamId: ' ' }]) {
    assert.deepEqual(getWorkbenchAccess(user), { canAccessWorkbench: false, canReview: false, canManageTeam: false })
  }
  assert.equal(getWorkbenchAccess({ role: 'employee', teamId: PREVIEW_TEAM_ID }).canReview, true)
  assert.equal(getWorkbenchAccess({ role: 'admin', teamId: PREVIEW_TEAM_ID }).canManageTeam, true)
})

test('preview selects an account; query labels never grant a role', () => {
  assert.equal(previewAccountId(employee, 'admin'), employee)
  assert.equal(previewAccountId('unknown', 'admin'), admin)
  const state = changeTeamRole(fresh(), admin, employee, 'admin', '轮值', now)
  const changed = changeTeamRole(state, admin, admin, 'employee', '交接', now)
  assert.equal(changed.members.find(member => member.id === previewAccountId(admin, 'admin')).role, 'employee')
})

test('review permission is assignment and state based, without admin bypass', () => {
  const state = fresh()
  const ticket = state.tickets.find(ticket => ticket.id.endsWith('-005'))
  assert.equal(canApproveTicket(ticket, admin, state.members), false)
  assert.equal(canApproveTicket(ticket, employee, state.members), true)
  assert.equal(canApproveTicket({ ...ticket, status: 'processing' }, employee, state.members), false)
  assert.equal(canApproveTicket({ ...ticket, status: 'completed' }, employee, state.members), false)
  assert.equal(canApproveTicket(ticket, employee, state.members.filter(member => member.id !== employee)), false)
  assert.equal(canApproveTicket({ ...ticket, reviewerId: admin }, admin, state.members), true)
})

test('employees cannot add, remove or change members and roles', () => {
  assert.throws(() => addTeamMember(fresh(), employee, 'member-song'), /仅团队管理员/)
  assert.throws(() => changeTeamRole(fresh(), employee, admin, 'employee', 'test'), /仅团队管理员/)
  assert.throws(() => removeTeamMember(fresh(), employee, 'member-xu', queue), /仅团队管理员/)
})

test('last admin cannot be demoted or removed and failed actions do not change state', () => {
  const state = fresh()
  const before = JSON.stringify(state)
  assert.throws(() => changeTeamRole(state, admin, admin, 'employee', 'test'), /至少一名管理员/)
  assert.throws(() => removeTeamMember(state, admin, admin, queue), /至少一名管理员/)
  assert.equal(JSON.stringify(state), before)
})

test('a second admin permits self-demotion; new role immediately controls actions', () => {
  let state = changeTeamRole(fresh(), admin, employee, 'admin', '团队轮值', now)
  state = changeTeamRole(state, admin, admin, 'employee', '交接', now)
  assert.equal(state.members.filter(member => member.role === 'admin').length, 1)
  assert.throws(() => addTeamMember(state, admin, 'member-song'), /仅团队管理员/)
  assert.equal(state.logs[0].before, '管理员')
  assert.equal(state.logs[0].after, '员工')
  assert.equal(state.logs[0].actorId, admin)
})

test('adding a person grants membership as employee and records the actor', () => {
  const state = addTeamMember(fresh(), admin, 'member-song', now)
  assert.equal(state.members.find(member => member.id === 'member-song').role, 'employee')
  assert.equal(state.logs[0].action, 'member_added')
  assert.equal(state.logs[0].actorName, '林晓')
  assert.throws(() => addTeamMember(state, admin, 'member-song'), /已经在团队/)
  assert.throws(() => addTeamMember(state, admin, 'unknown'), /未找到/)
})

test('admin reassigns another creator ticket with before/after names and reason', () => {
  const state = fresh()
  const updated = reassignTicket(state, admin, 'GP-20261024-002', { assigneeId: 'member-xu', reviewerId: employee, reason: '  周宁休假  ' }, now)
  assert.equal(updated.tickets[1].assigneeId, 'member-xu')
  assert.equal(updated.tickets[1].reviewerId, employee)
  assert.equal(updated.tickets[1].updatedAt, new Date(now).toISOString())
  assert.match(updated.logs[0].before, /周宁/)
  assert.match(updated.logs[0].after, /许清/)
  assert.equal(updated.logs[0].reason, '周宁休假')
  assert.equal(state.tickets[1].assigneeId, 'member-zhou')
})

test('creator can independently assign both responsibilities; unrelated employee cannot', () => {
  const assignment = { assigneeId: 'member-xu', reviewerId: employee, reason: '重新分工' }
  assert.throws(() => reassignTicket(fresh(), employee, first, assignment), /仅创建人或管理员/)
  const updated = reassignTicket(fresh(), employee, 'GP-20261024-002', assignment, now)
  assert.equal(updated.tickets[1].assigneeId, 'member-xu')
  assert.equal(updated.tickets[1].reviewerId, employee)
})

test('reassignment rejects missing reason, inactive targets, no changes and completed work', () => {
  const state = fresh()
  assert.throws(() => reassignTicket(state, admin, first, { assigneeId: 'member-xu', reviewerId: employee, reason: ' ' }), /原因/)
  assert.throws(() => reassignTicket(state, admin, first, { assigneeId: 'member-song', reviewerId: employee, reason: '交接' }), /不在团队/)
  assert.throws(() => reassignTicket(state, admin, first, { assigneeId: employee, reviewerId: 'member-xu', reason: '交接' }), /先调整/)
  assert.throws(() => reassignTicket(state, admin, 'GP-20261021-007', { assigneeId: employee, reviewerId: admin, reason: '交接' }), /已完成/)
  assert.equal(state.logs.length, 0)
})

test('removal queues only departed responsibilities, keeps completed history, and logs all changes', () => {
  const state = fresh()
  const affected = state.tickets.filter(ticket => ticket.status !== 'completed' && [ticket.assigneeId, ticket.reviewerId].includes('member-zhou'))
  const updated = removeTeamMember(state, admin, 'member-zhou', queue, now)
  assert.equal(updated.members.some(member => member.id === 'member-zhou'), false)
  for (const old of affected) {
    const ticket = updated.tickets.find(ticket => ticket.id === old.id)
    assert.equal(ticket.assigneeId, old.assigneeId === 'member-zhou' ? null : old.assigneeId)
    assert.equal(ticket.reviewerId, old.reviewerId === 'member-zhou' ? null : old.reviewerId)
    assert.equal(getUnassignedSides(ticket, updated.members).assignee || getUnassignedSides(ticket, updated.members).reviewer, true)
  }
  assert.deepEqual(updated.tickets.filter(ticket => ticket.status === 'completed'), state.tickets.filter(ticket => ticket.status === 'completed'))
  assert.equal(updated.logs.length, affected.length + 1)
  assert.equal(updated.logs[0].action, 'member_removed')
})

test('removal can hand off processing and reviewing separately in one transaction', () => {
  const state = removeTeamMember(fresh(), admin, 'member-zhou', { mode: 'reassign', assigneeId: 'member-xu', reviewerId: employee, reason: '工作交接' }, now)
  assert.equal(state.tickets[1].assigneeId, 'member-xu')
  assert.equal(state.tickets[2].reviewerId, employee)
  assert.equal(state.tickets[1].reviewerId, admin)
  assert.equal(state.tickets[3].creatorId, 'member-zhou')
})

test('invalid handoff fails atomically before membership, assignments or logs change', () => {
  const state = fresh()
  const snapshot = JSON.stringify(state)
  for (const plan of [
    { mode: 'reassign', assigneeId: 'member-xu', reviewerId: null, reason: '交接' },
    { mode: 'reassign', assigneeId: 'member-zhou', reviewerId: employee, reason: '交接' },
    { mode: 'reassign', assigneeId: 'member-song', reviewerId: employee, reason: '交接' },
    { ...queue, reason: '' },
  ]) {
    assert.throws(() => removeTeamMember(state, admin, 'member-zhou', plan, now))
    assert.equal(JSON.stringify(state), snapshot)
  }
})

test('pending queue excludes completed history; stale means unfinished and >= 7 days', () => {
  const state = fresh()
  assert.deepEqual(getUnassignedSides({ ...state.tickets[0], assigneeId: null, reviewerId: null }, state.members), { assignee: true, reviewer: true })
  assert.deepEqual(getUnassignedSides({ ...state.tickets[0], status: 'completed', assigneeId: null, reviewerId: null }, state.members), { assignee: false, reviewer: false })
  assert.equal(isStaleTicket({ ...state.tickets[0], updatedAt: new Date(now - 7 * 86400000).toISOString() }, now), true)
  assert.equal(isStaleTicket(state.tickets[6], now), false)
})

test('a removed admin loses access, snapshots remain and rejoining grants employee role', () => {
  let state = changeTeamRole(fresh(), admin, employee, 'admin', '交接', now)
  state = removeTeamMember(state, employee, admin, queue, now)
  assert.throws(() => addTeamMember(state, admin, 'member-song'), /不在团队/)
  assert.equal(state.logs.find(log => log.action === 'role_changed').actorName, '林晓')
  state = addTeamMember(state, employee, admin, now)
  assert.equal(state.members.find(member => member.id === admin).role, 'employee')
})
