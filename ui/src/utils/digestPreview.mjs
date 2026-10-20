export function getDigestPreviewContent(notification = {}) {
  const rawContent = typeof notification.content === 'string' ? notification.content : ''
  const trimmed = rawContent.trim()
  if (!trimmed) return { content: notification.title || '摘要正文不可用', markdown: false }

  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const key of ['display', 'digest', 'summary', 'text', 'content']) {
          if (typeof parsed[key] === 'string' && parsed[key].trim()) {
            return { content: parsed[key], markdown: true }
          }
        }
      }
    } catch {
      // Keep malformed JSON visible as plain text instead of hiding the record.
    }
    return { content: rawContent, markdown: false }
  }

  return { content: rawContent, markdown: true }
}
