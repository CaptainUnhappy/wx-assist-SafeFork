import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Bell, Power } from '@phosphor-icons/react'
import { API_BASE } from './SharedComponents'

/**
 * 关窗确认弹窗。
 *
 * 触发方式：desktop.py 拦截到「窗口关闭」后，会通过 evaluate_js 调
 * window.__wxShowCloseDialog()，这里就把弹窗显示出来。
 * 用户点完选择后回传 /api/app/close-intent，由 Python 端执行
 * 「隐藏窗口到托盘」或「真正退出」。
 *
 * 注意：Python 端只等 3 秒，超时会退到系统弹窗兜底 ——
 * 所以这个组件必须尽早注册 __wxShowCloseDialog，不能依赖用户交互。
 */
export default function CloseConfirmDialog() {
  const [visible, setVisible] = useState(false)
  const [remember, setRemember] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    window.__wxShowCloseDialog = () => {
      setRemember(false)
      setBusy(false)
      setVisible(true)
    }
    return () => {
      if (window.__wxShowCloseDialog) delete window.__wxShowCloseDialog
    }
  }, [])

  async function choose(action) {
    if (busy) return
    setBusy(true)
    try {
      await fetch(`${API_BASE}/api/app/close-intent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, remember }),
      })
    } catch {
      // 请求失败也别拦着用户：关不关得掉由 Python 端的兜底逻辑决定
    }
    setVisible(false)
    setBusy(false)
  }

  return (
    <AnimatePresence>
      {visible ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-[70] flex items-center justify-center bg-bg-main/80 dark:bg-black/70 backdrop-blur-sm p-4"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96 }}
            transition={{ type: 'spring', stiffness: 300, damping: 26 }}
            className="bg-bg-card border border-border-main rounded-2xl shadow-2xl p-7 w-[440px] max-w-full"
          >
            <h3 className="text-base font-semibold tracking-tight text-text-main">
              要关闭「摘星 · 微信助手」吗？
            </h3>

            <p className="text-[13px] text-text-muted mt-2.5 leading-relaxed">
              关掉窗口后程序仍在后台运行：
            </p>
            <div className="mt-2 space-y-1.5">
              <p className="flex gap-2 text-[13px] text-text-muted leading-relaxed">
                <Bell size={13} className="mt-1 shrink-0 text-brand-green" />
                <span>关键词提醒会继续推送</span>
              </p>
              <p className="flex gap-2 text-[13px] text-text-muted leading-relaxed">
                <Power size={13} className="mt-1 shrink-0 text-brand-green" />
                <span>定时任务、群摘要会继续执行</span>
              </p>
            </div>

            <div className="flex gap-3 mt-6">
              <motion.button
                whileTap={{ scale: 0.97 }}
                onClick={() => choose('tray')}
                disabled={busy}
                className="flex-1 py-2.5 rounded-full bg-brand-green-hover text-white text-[14px] font-semibold hover:bg-[#0d8c5c] transition-colors cursor-pointer disabled:opacity-60"
              >
                最小化到托盘
              </motion.button>
              <motion.button
                whileTap={{ scale: 0.97 }}
                onClick={() => choose('quit')}
                disabled={busy}
                className="flex-1 py-2.5 rounded-full border border-border-main text-text-main text-[14px] font-medium hover:bg-bg-raised transition-colors cursor-pointer disabled:opacity-60"
              >
                退出程序
              </motion.button>
            </div>

            <label className="flex items-center gap-2 mt-5 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={remember}
                onChange={e => setRemember(e.target.checked)}
                className="w-4 h-4 cursor-pointer accent-[var(--brand-green)]"
              />
              <span className="text-[13px] text-text-muted">记住我的选择，下次不再询问</span>
            </label>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}
