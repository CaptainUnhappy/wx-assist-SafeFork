const FALLBACK_FIELDS = ['display', 'digest', 'summary', 'text', 'content']

function parseContent(notification = {}) {
  const raw = typeof notification.content === 'string' ? notification.content.trim() : ''
  if (!raw || !raw.startsWith('{')) return { data: null, raw }
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { data: parsed, raw }
    }
  } catch {
    // Malformed JSON stays visible as plain text instead of hiding the record.
  }
  return { data: null, raw }
}

function pickText(data, fields = FALLBACK_FIELDS) {
  if (!data) return ''
  for (const key of fields) {
    if (typeof data[key] === 'string' && data[key].trim()) return data[key].trim()
  }
  return ''
}

function joinBlocks(blocks) {
  return blocks.filter(Boolean).join('\n\n')
}

/**
 * Turn one outbox record into readable text for the notification list and the
 * preview dialog. Each notification type stores a different JSON payload, so
 * the card never shows the raw payload.
 */
export function getNotificationPreview(notification = {}) {
  const { data, raw } = parseContent(notification)
  const fallback = notification.title || notification.group_name || '正文不可用'

  if (!data) {
    // cron text and legacy plain-text records are already readable.
    return { content: raw || fallback, markdown: Boolean(raw) }
  }

  switch (notification.type) {
    case 'keyword_alert': {
      // display already carries sender/message/keywords plus the matched
      // snippet for regex rules, so prefer it over reassembling the fields.
      const message = pickText(data, ['message'])
      const head = [data.sender, data.group].filter(Boolean).join(' · ')
      const keywords = Array.isArray(data.keywords) ? data.keywords.filter(Boolean) : []
      const composed = joinBlocks([
        head && message ? `${head}：${message}` : message,
        keywords.length ? `关键词：${keywords.join('、')}` : '',
      ])
      return { content: pickText(data, ['display']) || composed || fallback, markdown: true }
    }
    case 'oa_article_alert': {
      const body = pickText(data, ['digest', 'display'])
      const link = data.url ? `[原文链接](${data.url})` : ''
      return { content: joinBlocks([body, link]) || fallback, markdown: true }
    }
    case 'group_digest': {
      const body = pickText(data, ['digest', 'display'])
      const meta = [
        data.msg_count ? `共 ${data.msg_count} 条消息` : '',
        data.lookback_hours ? `近 ${data.lookback_hours} 小时` : '',
        data.degraded ? '已降级' : '',
      ].filter(Boolean).join(' · ')
      return { content: joinBlocks([body, meta]) || fallback, markdown: true }
    }
    case 'oa_digest': {
      const body = pickText(data, ['digest', 'display'])
      const meta = data.articles_count ? `共 ${data.articles_count} 篇文章` : ''
      return { content: joinBlocks([body, meta]) || fallback, markdown: true }
    }
    default: {
      const body = pickText(data)
      return { content: body || fallback, markdown: Boolean(body) }
    }
  }
}

// List cards show the summary as plain text: markdown markers are stripped so a
// truncated preview never exposes "**" or ">" fragments.
export function toPlainPreview(markdown = '') {
  return String(markdown ?? '')
    .replace(/\r\n/g, '\n')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '· ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
