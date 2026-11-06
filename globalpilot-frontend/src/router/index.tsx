import { createBrowserRouter } from 'react-router'
import AppLayout from '../layout/AppLayout'
import Home from '../pages/Home'
import Assets from '../pages/Assets'
import AssetPreview from '../pages/Assets/Preview'
import Login from '../pages/Login'
import Register from '../pages/Register'
import WorkbenchShell, { WorkbenchBlankPage, WorkbenchPermissionGate, WorkbenchReviewGate } from '../workbench/WorkbenchShell'
import Workbench from '../pages/Workbench'
import TicketCreate from '../pages/TicketCreate'
import TicketList from '../pages/TicketList'
import TicketDetail from '../pages/TicketDetail'
import Team from '../pages/Team'
import TeamTickets from '../pages/TeamTickets'
import OperationLogs from '../pages/OperationLogs'
import {
  ProtectedEval,
  ProtectedMarketing,
  ProtectedProfile,
  ProtectedResearch,
} from './protected'

export const router = createBrowserRouter([
  {
    path: '/workbench',
    element: <WorkbenchShell />,
    children: [
      { index: true, Component: Workbench },
      { path: 'tickets', Component: TicketList },
      { path: 'tickets/new', Component: TicketCreate },
      { path: 'tickets/:id', Component: TicketDetail },
      { path: 'reviews', element: <WorkbenchPermissionGate permission="review" /> },
      { path: 'reviews/:id', Component: WorkbenchReviewGate },
      { path: 'team', element: <WorkbenchPermissionGate permission="team"><Team /></WorkbenchPermissionGate> },
      { path: 'team/tickets', element: <WorkbenchPermissionGate permission="team"><TeamTickets /></WorkbenchPermissionGate> },
      { path: 'team/unassigned', element: <WorkbenchPermissionGate permission="team"><TeamTickets mode="unassigned" /></WorkbenchPermissionGate> },
      { path: 'team/logs', element: <WorkbenchPermissionGate permission="team"><OperationLogs /></WorkbenchPermissionGate> },
      { path: 'marketing', Component: WorkbenchBlankPage },
      { path: 'marketing/:id', Component: WorkbenchBlankPage },
      { path: 'assets', Component: WorkbenchBlankPage },
    ],
  },
  ...(import.meta.env.DEV ? [{
    path: '/preview/workbench',
    element: <WorkbenchShell preview />,
    children: [
      { index: true, Component: Workbench },
      { path: 'tickets', Component: WorkbenchBlankPage },
      { path: 'tickets/new', Component: WorkbenchBlankPage },
      { path: 'tickets/:id', Component: WorkbenchBlankPage },
      { path: 'reviews', element: <WorkbenchPermissionGate permission="review" /> },
      { path: 'reviews/:id', Component: WorkbenchReviewGate },
      { path: 'team', element: <WorkbenchPermissionGate permission="team"><Team /></WorkbenchPermissionGate> },
      { path: 'team/tickets', element: <WorkbenchPermissionGate permission="team"><TeamTickets /></WorkbenchPermissionGate> },
      { path: 'team/unassigned', element: <WorkbenchPermissionGate permission="team"><TeamTickets mode="unassigned" /></WorkbenchPermissionGate> },
      { path: 'team/logs', element: <WorkbenchPermissionGate permission="team"><OperationLogs /></WorkbenchPermissionGate> },
      { path: 'marketing', Component: WorkbenchBlankPage },
      { path: 'marketing/:id', Component: WorkbenchBlankPage },
      { path: 'assets', Component: WorkbenchBlankPage },
    ],
  }] : []),
  {
    path: '/',
    Component: AppLayout,
    children: [
      { index: true, Component: Home },
      { path: 'research', Component: ProtectedResearch },
      { path: 'marketing', Component: ProtectedMarketing },
      { path: 'eval', Component: ProtectedEval },
      { path: 'assets', Component: Assets },
      { path: 'assets/:documentId', Component: AssetPreview },
      { path: 'settings', Component: ProtectedProfile },
      { path: 'login', Component: Login },
      { path: 'register', Component: Register },
    ],
  },
])
