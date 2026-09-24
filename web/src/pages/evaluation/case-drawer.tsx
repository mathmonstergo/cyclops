import { useState, type ReactNode } from 'react'
import { Save } from 'lucide-react'
import type { RetrievalEvalCase, RetrievalEvalCaseRecord } from '@/api/schemas'
import { Button } from '@/components/ui/button'
import {
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { DRAWER_WIDTH_COMPACT } from '@/components/ui/drawer-constants'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from '@/components/ui/toast'
import { useSaveRetrievalEvalCase } from '@/api/hooks'
import { CASE_FORM_STATUS_OPTIONS, joinListInput, splitListInput } from './helpers'

interface CaseFormState {
  id: string
  question: string
  intent: string
  expectedSourceIds: string
  expectedChunkIds: string
  tags: string
  note: string
  status: string
}

const EMPTY_FORM: CaseFormState = {
  id: '',
  question: '',
  intent: '',
  expectedSourceIds: '',
  expectedChunkIds: '',
  tags: '',
  note: '',
  status: 'active',
}

// 用例编辑抽屉容器；open 时才挂载表单，避免关闭后残留旧输入。
export function CaseDrawer({
  open,
  item,
  onOpenChange,
  onSaved,
}: {
  open: boolean
  item: RetrievalEvalCase | null
  onOpenChange: (open: boolean) => void
  onSaved: (item: RetrievalEvalCaseRecord) => void
}) {
  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      {open && (
        <CaseDrawerForm
          key={item?.id || 'new'}
          item={item}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Drawer>
  )
}

// 将后端用例快照转换为表单字符串，列表字段在 UI 中以换行展示。
function formStateFromItem(item: RetrievalEvalCase | null): CaseFormState {
  if (!item) return EMPTY_FORM
  return {
    id: item.id,
    question: item.question,
    intent: item.intent || '',
    expectedSourceIds: joinListInput(item.expected_source_ids),
    expectedChunkIds: joinListInput(item.expected_chunk_ids),
    tags: joinListInput(item.tags),
    note: item.note || '',
    status: item.status || 'active',
  }
}

// 用例表单主体；负责保存输入并把列表字段转换回数组提交给后端。
function CaseDrawerForm({
  item,
  onOpenChange,
  onSaved,
}: {
  item: RetrievalEvalCase | null
  onOpenChange: (open: boolean) => void
  onSaved: (item: RetrievalEvalCaseRecord) => void
}) {
  const [form, setForm] = useState<CaseFormState>(() => formStateFromItem(item))
  const saveCase = useSaveRetrievalEvalCase()

  // 单字段更新表单状态，所有字段保持受控输入。
  const update = (key: keyof CaseFormState, value: string) => {
    setForm((current) => ({ ...current, [key]: value }))
  }

  // 保存评测用例；后端统一做 id 生成、JSONB 清洗和 upsert。
  const onSubmit = async () => {
    try {
      const saved = await saveCase.mutateAsync({
        id: form.id || undefined,
        question: form.question,
        intent: form.intent || null,
        expected_source_ids: splitListInput(form.expectedSourceIds),
        expected_chunk_ids: splitListInput(form.expectedChunkIds),
        tags: splitListInput(form.tags),
        note: form.note || null,
        status: form.status,
      })
      toast.success('评测用例已保存')
      onSaved(saved)
      onOpenChange(false)
    } catch (error) {
      toast.error((error as Error).message || '保存评测用例失败')
    }
  }

  return (
    <DrawerContent width={DRAWER_WIDTH_COMPACT}>
      <DrawerHeader>
        <div>
          <DrawerTitle>{item ? '编辑评测用例' : '新建评测用例'}</DrawerTitle>
          <p className="mt-1 text-[12px] text-(--color-text-muted)">
            先填写问题并运行评测，再从候选来源中标注期望命中；内部 ID 输入仅用于高级排查。
          </p>
        </div>
      </DrawerHeader>
      <DrawerBody>
        <div className="space-y-4">
          <Field label="问题" required>
            <Textarea
              value={form.question}
              onChange={(event) => update('question', event.target.value)}
              placeholder="报告导出失败怎么办？"
              className="min-h-20 font-sans"
              maxLength={200}
            />
          </Field>
          <Field label="意图">
            <Input
              value={form.intent}
              onChange={(event) => update('intent', event.target.value)}
              placeholder="troubleshooting"
            />
          </Field>
          <details className="rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) px-3 py-2">
            <summary className="cursor-pointer select-none text-[12px] text-(--color-text-muted) [&::-webkit-details-marker]:hidden">
              高级：手动填写期望 ID
            </summary>
            <div className="mt-3 space-y-3">
              <Field label="期望来源 ID">
                <Textarea
                  value={form.expectedSourceIds}
                  onChange={(event) => update('expectedSourceIds', event.target.value)}
                  placeholder="faq_2056&#10;imp_10023"
                />
              </Field>
              <Field label="期望切片 ID（可选）">
                <Textarea
                  value={form.expectedChunkIds}
                  onChange={(event) => update('expectedChunkIds', event.target.value)}
                  placeholder="kc_faq_2056&#10;kc_doc_child_10023"
                />
              </Field>
              <p className="text-[11px] leading-5 text-(--color-text-faint)">
                一般不用手填。运行用例后，在右侧候选来源里点击“设为期望来源 / 设为期望切片”会自动写入正确 ID。
              </p>
            </div>
          </details>
          <Field label="标签">
            <Input
              value={form.tags}
              onChange={(event) => update('tags', event.target.value)}
              placeholder="报告导出, 故障处理"
            />
          </Field>
          <Field label="备注">
            <Textarea
              value={form.note}
              onChange={(event) => update('note', event.target.value)}
              placeholder="客户反馈导出失败，需能命中原因与解决方法。"
              className="min-h-20 font-sans"
            />
          </Field>
          <Field label="状态">
            <select
              value={form.status}
              onChange={(event) => update('status', event.target.value)}
              className="h-8 w-full rounded-(--radius-control) border border-(--color-border) bg-(--color-surface-2) px-2.5 text-[13px] text-(--color-text) focus:border-(--color-primary)/60 focus:outline-none"
            >
              {CASE_FORM_STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </DrawerBody>
      <DrawerFooter>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          取消
        </Button>
        <Button
          variant="primary"
          onClick={onSubmit}
          disabled={saveCase.isPending || !form.question.trim()}
        >
          <Save className="size-3.5" />
          保存
        </Button>
      </DrawerFooter>
    </DrawerContent>
  )
}

// 表单字段包装器；统一标签、必填标识和输入控件的垂直间距。
function Field({
  label,
  required,
  children,
}: {
  label: string
  required?: boolean
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[12px] text-(--color-text-muted)">
        {label}
        {required && <span className="ml-0.5 text-(--color-danger)">*</span>}
      </span>
      {children}
    </label>
  )
}
