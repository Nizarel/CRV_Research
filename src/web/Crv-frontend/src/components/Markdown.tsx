import type { ReactNode } from 'react'

/** Renders the Markdown subset the answer model writes: paragraphs, bullet and numbered lists,
 * headings, GFM tables, **bold**, *italic*, `code` and `[<evidence id>]` citation markers.
 * Everything is emitted as React text nodes, so model output can never inject HTML. */

/** Renders one bracketed marker; must return the bracketed text itself for unknown ids. */
export type RenderRef = (ref: string, key: string) => ReactNode

type Block =
  | { type: 'paragraph'; lines: string[] }
  | { type: 'list'; ordered: boolean; start: number; items: string[] }
  | { type: 'heading'; text: string }
  | { type: 'table'; header: string[]; rows: string[][] }

const BULLET = /^\s*[-*•]\s+(.*)$/
const ORDERED = /^\s*(\d{1,3})[.)]\s+(.*)$/
const HEADING = /^\s*#{1,6}\s+(.*)$/
const TABLE_ROW = /^\s*\|.*\|\s*$/
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/
const INLINE =
  /(`[^`\n]+`)|(\*\*(?=\S)([\s\S]*?\S)\*\*)|((?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![\w*]))|(\[([^[\]\n]+)\])/g

function cells(row: string): string[] {
  return row
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

function startsBlock(lines: string[], index: number): boolean {
  const line = lines[index]
  return (
    BULLET.test(line) ||
    ORDERED.test(line) ||
    HEADING.test(line) ||
    (TABLE_ROW.test(line) && TABLE_SEPARATOR.test(lines[index + 1] ?? ''))
  )
}

function parseBlocks(text: string): Block[] {
  const lines = text.split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i += 1
      continue
    }
    const heading = line.match(HEADING)
    if (heading) {
      blocks.push({ type: 'heading', text: heading[1] })
      i += 1
      continue
    }
    if (TABLE_ROW.test(line) && TABLE_SEPARATOR.test(lines[i + 1] ?? '')) {
      const header = cells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(cells(lines[i]))
        i += 1
      }
      blocks.push({ type: 'table', header, rows })
      continue
    }
    const ordered = line.match(ORDERED)
    if (BULLET.test(line) || ordered) {
      const pattern = ordered ? ORDERED : BULLET
      const items: string[] = []
      while (i < lines.length) {
        const item = lines[i].match(pattern)
        if (item) {
          items.push(ordered ? item[2] : item[1])
        } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && !startsBlock(lines, i)) {
          items[items.length - 1] += `\n${lines[i].trim()}`
        } else {
          break
        }
        i += 1
      }
      blocks.push({ type: 'list', ordered: Boolean(ordered), start: ordered ? Number(ordered[1]) : 1, items })
      continue
    }
    const paragraph: string[] = []
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      paragraph.push(lines[i].trim())
      i += 1
    }
    blocks.push({ type: 'paragraph', lines: paragraph })
  }
  return blocks
}

function defaultRef(ref: string): ReactNode {
  return `[${ref}]`
}

function renderInline(text: string, renderRef: RenderRef = defaultRef, prefix = 'i'): ReactNode[] {
  const nodes: ReactNode[] = []
  let last = 0
  let index = 0
  // A `**` left over after pairing is an unclosed run (often mid-stream): never show it.
  const plain = (value: string) => value.replace(/\*\*/g, '')
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0
    if (start > last) nodes.push(plain(text.slice(last, start)))
    const key = `${prefix}-${index++}`
    if (match[1]) {
      nodes.push(<code key={key}>{match[1].slice(1, -1)}</code>)
    } else if (match[2]) {
      nodes.push(<strong key={key}>{renderInline(match[3], renderRef, key)}</strong>)
    } else if (match[4]) {
      nodes.push(<em key={key}>{renderInline(match[5], renderRef, key)}</em>)
    } else {
      nodes.push(renderRef(match[7], key))
    }
    last = start + match[0].length
  }
  if (last < text.length) nodes.push(plain(text.slice(last)))
  return nodes
}

interface MarkdownProps {
  text: string
  renderRef?: RenderRef
}

/** One line of Markdown without block structure (no paragraphs or lists). */
export function InlineMarkdown({ text, renderRef = defaultRef }: MarkdownProps) {
  return <>{renderInline(text, renderRef)}</>
}

export function Markdown({ text, renderRef = defaultRef }: MarkdownProps) {
  return (
    <>
      {parseBlocks(text).map((block, blockIndex) => {
        const key = `b${blockIndex}`
        switch (block.type) {
          case 'heading':
            return (
              <p key={key} className="md-heading">
                {renderInline(block.text, renderRef, key)}
              </p>
            )
          case 'list': {
            const items = block.items.map((item, itemIndex) => (
              <li key={itemIndex}>{renderLines(item.split('\n'), renderRef, `${key}-${itemIndex}`)}</li>
            ))
            return block.ordered ? (
              <ol key={key} start={block.start}>
                {items}
              </ol>
            ) : (
              <ul key={key}>{items}</ul>
            )
          }
          case 'table':
            return (
              <div key={key} className="md-table-wrap">
                <table className="md-table">
                  <thead>
                    <tr>
                      {block.header.map((cell, cellIndex) => (
                        <th key={cellIndex}>{renderInline(cell, renderRef, `${key}-h${cellIndex}`)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={rowIndex}>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>
                            {renderInline(cell, renderRef, `${key}-${rowIndex}-${cellIndex}`)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          default:
            return <p key={key}>{renderLines(block.lines, renderRef, key)}</p>
        }
      })}
    </>
  )
}

function renderLines(lines: string[], renderRef: RenderRef, key: string): ReactNode[] {
  return lines.flatMap((line, lineIndex) => [
    ...(lineIndex > 0 ? [<br key={`${key}-br${lineIndex}`} />] : []),
    ...renderInline(line, renderRef, `${key}-l${lineIndex}`),
  ])
}
