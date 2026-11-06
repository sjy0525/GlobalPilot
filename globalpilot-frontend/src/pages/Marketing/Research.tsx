import type { ResearchReport, ResearchValidation } from '../../api/marketing'

export function ResearchCard({
  report,
  validation,
}: {
  report: ResearchReport
  validation?: ResearchValidation | null
}) {
  const verified = validation?.passed === true
  const limited = report.status === 'insufficient' || !verified
  const title = report.status === 'not_needed' && verified
    ? 'Existing brand facts were enough'
    : report.status === 'completed' && verified
      ? 'Research evidence checked'
      : 'Research has gaps'
  const issues = [...new Set([...report.uncertainties, ...(validation?.issues ?? [])])]
  const sources = report.source_urls.filter(url => {
    try {
      return ['http:', 'https:'].includes(new URL(url).protocol)
    } catch {
      return false
    }
  })

  return (
    <article className={`gp-panel gp-marketing__card gp-marketing__audit${limited ? ' is-blocked' : ''}`}>
      <p className="gp-marketing__card-kicker">Research</p>
      <h2 className="gp-panel__title">{title}</h2>
      <p className="gp-panel__meta">
        {verified
          ? 'Evidence was reviewed for this run. Sources and remaining limitations are below.'
          : 'The research did not pass evidence checks. External findings should not be treated as verified facts.'}
      </p>
      <p className="gp-marketing__blocker-line">{report.summary}</p>
      {sources.length ? (
        <>
          <h3>Sources</h3>
          <ul>{sources.map(url => (
            <li key={url}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a></li>
          ))}</ul>
        </>
      ) : (
        <p className="gp-panel__meta">No verified external sources were used.</p>
      )}
      {issues.length ? (
        <>
          <h3>Gaps and limitations</h3>
          <ul>{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
        </>
      ) : null}
    </article>
  )
}
