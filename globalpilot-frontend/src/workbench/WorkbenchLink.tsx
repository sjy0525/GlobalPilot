import { Link, parsePath, type LinkProps } from 'react-router'
import { useWorkbench } from './context'

/** Keep preview selection in navigation URLs without altering real routes. */
export default function WorkbenchLink({ to, ...props }: LinkProps) {
  const { basePath, isPreview, currentMemberId } = useWorkbench()
  const path = typeof to === 'string' ? parsePath(to) : to
  if (!isPreview || !path.pathname?.startsWith(basePath)) return <Link to={to} {...props} />
  const params = new URLSearchParams(path.search)
  params.set('member', currentMemberId)
  params.delete('view')
  return <Link to={{ ...path, search: `?${params.toString()}` }} {...props} />
}
