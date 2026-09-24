// 底部输入框：自动增高 Textarea + 发送/停止按钮。
// Cmd/Ctrl + Enter 发送，回车换行；流式中按钮变为"停止"。Esc 停止流式。
import { useEffect, useRef } from 'react'
import { ArrowUp, Square } from 'lucide-react'
import { cn } from '@/lib/cn'

interface Props {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onAbort: () => void
  isStreaming: boolean
  disabled?: boolean
  placeholderHint?: string
}

export function Composer({
  value,
  onChange,
  onSend,
  onAbort,
  isStreaming,
  disabled,
  placeholderHint,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // 自动增高
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = '0px'
    const next = Math.min(el.scrollHeight, 260)
    el.style.height = next + 'px'
  }, [value])

  const canSend = !!value.trim() && !disabled && !isStreaming

  return (
    <div className="border-t border-(--color-border) bg-(--color-bg)">
      <div className="mx-auto max-w-3xl px-4 py-4">
        <div
          className={cn(
            'flex items-end gap-2 rounded-(--radius-card) border border-(--color-border)',
            'bg-(--color-surface) px-3 py-2.5 transition-colors',
            'focus-within:border-(--color-primary)/40',
          )}
        >
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              // Enter 发送 / Shift+Enter 换行（仿 ChatGPT/Claude）。
              // Cmd/Ctrl+Enter 同样发送；输入法组合（compositionend 期间的 Enter）不触发。
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                if (canSend) onSend()
              } else if (e.key === 'Escape' && isStreaming) {
                e.preventDefault()
                onAbort()
              }
            }}
            placeholder={
              isStreaming ? '生成中… 按 Esc 停止' : '问点什么吧，Enter 发送 · Shift+Enter 换行'
            }
            disabled={disabled}
            rows={1}
            className={cn(
              'min-h-6 flex-1 resize-none bg-transparent text-[14px] text-(--color-text)',
              'leading-[1.6] outline-none placeholder:text-(--color-text-faint)',
              'disabled:opacity-50',
            )}
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={onAbort}
              className={cn(
                'inline-flex size-8 shrink-0 items-center justify-center rounded-full',
                'bg-(--color-text) text-(--color-bg)',
                'transition-transform active:scale-95',
              )}
              aria-label="停止生成"
              title="停止生成（Esc）"
            >
              <Square className="size-3.5" fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              className={cn(
                'inline-flex size-8 shrink-0 items-center justify-center rounded-full',
                'transition-all active:scale-95',
                canSend
                  ? 'bg-(--color-primary) text-white hover:bg-(--color-primary-hi)'
                  : 'bg-(--color-surface-2) text-(--color-text-faint)',
              )}
              aria-label="发送"
              title="发送（Enter）"
            >
              <ArrowUp className="size-4" />
            </button>
          )}
        </div>
        {placeholderHint && (
          <div className="mt-2 text-center text-[11px] text-(--color-text-faint)">
            {placeholderHint}
          </div>
        )}
      </div>
    </div>
  )
}
