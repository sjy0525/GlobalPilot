function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function renderInline(value: string) {
  return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

export function renderMarketingMarkdown(source: string) {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let list: 'ul' | 'ol' | null = null

  const closeList = () => {
    if (!list) return
    out.push(list === 'ul' ? '</ul>' : '</ol>')
    list = null
  }

  const openList = (next: 'ul' | 'ol') => {
    if (list === next) return
    closeList()
    out.push(next === 'ul' ? '<ul>' : '<ol>')
    list = next
  }

  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) {
      closeList()
      continue
    }
    if (/^---+$/.test(trimmed)) {
      closeList()
      out.push('<hr />')
      continue
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(trimmed)
    if (heading) {
      closeList()
      const level = Math.min(heading[1].length, 3)
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`)
      continue
    }
    const ul = /^[-*•·]\s+(.+)$/.exec(trimmed)
    if (ul) {
      openList('ul')
      out.push(`<li>${renderInline(ul[1])}</li>`)
      continue
    }
    const ol = /^\d+[.)]\s+(.+)$/.exec(trimmed)
    if (ol) {
      openList('ol')
      out.push(`<li>${renderInline(ol[1])}</li>`)
      continue
    }
    closeList()
    out.push(`<p>${renderInline(trimmed)}</p>`)
  }
  closeList()
  return out.join('')
}

function MarketingProse({ text, empty }: { text: string | null | undefined; empty: string }) {
  const source = text?.trim()
  if (!source) {
    return <p className="gp-marketing__prose-empty">{empty}</p>
  }
  return (
    <div
      className="gp-marketing__prose"
      dangerouslySetInnerHTML={{ __html: renderMarketingMarkdown(source) }}
    />
  )
}

export default MarketingProse
