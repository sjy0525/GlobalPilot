import type { MarketingCopy } from '../../api/marketing'

const SOCIAL_LABELS: Record<string, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  youtube: 'YouTube',
  linkedin: 'LinkedIn',
  facebook: 'Facebook',
  x: 'X',
}

const FIELD_LABELS = [
  ['hook', 'Hook'] as const,
  ['body', 'Body'] as const,
  ['cta', 'CTA'] as const,
  ['tags', 'Tags'] as const,
]

function ChannelCopy({
  content,
  empty,
}: {
  content: MarketingCopy | null | undefined
  empty: string
}) {
  const entries = Object.entries(content?.platforms ?? {})
  if (!entries.length) {
    return <p className="gp-marketing__prose-empty">{empty}</p>
  }
  return (
    <div className="gp-marketing__channels">
      {entries.map(([platform, copy]) => (
        <section key={platform} className="gp-marketing__channel">
          <h3>{SOCIAL_LABELS[platform] ?? platform}</h3>
          {FIELD_LABELS.map(([key, label]) => (
            <p key={key}>
              <span>{label}</span>
              {copy[key] || '—'}
            </p>
          ))}
        </section>
      ))}
    </div>
  )
}

export default ChannelCopy
