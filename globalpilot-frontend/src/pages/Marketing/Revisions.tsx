import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../../api/http'
import type { BrandDocument } from '../../api/documents'
import {
  clarifyRevisionTask,
  createRevisionTask,
  exportRevisionSample,
  getRevisionTask,
  listRevisionEvidenceDocuments,
  listRevisionTasks,
  readRevisionDocument,
  reviewRevisionTask,
  runRevisionTask,
  type DraftSnapshot,
  type RevisionCreateRequest,
  type RevisionTaskOutput,
} from '../../api/revisions'
import {
  buildRevisionReviewBlocks,
  clearPendingRevisionOperation,
  countRevisionText,
  newRevisionKey,
  readPendingRevisionOperation,
  revisionCommand,
  revisionStatusLabel,
  savePendingRevisionOperation,
  type RevisionPendingOperation,
} from '../../workbench/revisions'

type RevisionProps = {
  briefId: string
  teamId: string
  userId: string
  brandId: string
  platforms: string[]
  disabled?: boolean
  onAdopted: () => void | Promise<void>
  onBusyChange?: (busy: boolean) => void
}

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube',
  linkedin: 'LinkedIn', facebook: 'Facebook', x: 'X',
}
const LIVE_STATUSES = new Set(['running', 'adopting'])

function lines(value: string): string[] {
  return value.split(/\r?\n/).filter(item => item.trim().length > 0)
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '操作未完成，请稍后重试。'
}

async function readSource(
  briefId: string,
  platform: string,
  teamId: string,
  signal: AbortSignal,
): Promise<DraftSnapshot> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await readRevisionDocument(briefId, platform, teamId, signal)
    } catch (error) {
      if (attempt >= 2 || signal.aborted || !(error instanceof ApiError)
        || error.status !== 409 || error.code !== 'CONFLICT'
        || !/正在处理中.*稍后重试/.test(error.message)) throw error
      // StrictMode can abort one source read while its server lock is briefly
      // finishing. Retry only this readonly busy response, never a version error.
      await new Promise<void>((resolve, reject) => {
        const cancel = () => {
          window.clearTimeout(timer)
          reject(new DOMException('Source read aborted', 'AbortError'))
        }
        const timer = window.setTimeout(() => {
          signal.removeEventListener('abort', cancel)
          resolve()
        }, 150 * (attempt + 1))
        signal.addEventListener('abort', cancel, { once: true })
        if (signal.aborted) cancel()
      })
    }
  }
}

function selectedStorageKey(userId: string, teamId: string, briefId: string) {
  return ['gp-revision-selection', userId, teamId, briefId].join(':')
}

function rememberSelection(key: string, taskId: string | null) {
  try {
    if (taskId) window.localStorage.setItem(key, taskId)
    else window.localStorage.removeItem(key)
  } catch {
    // Selection is only a convenience; pending mutation storage is mandatory.
  }
}

function pendingTaskId(operation: RevisionPendingOperation | null): string | null {
  return operation && operation.kind !== 'create' ? operation.task_id : null
}

function editableTexts(task: RevisionTaskOutput, previous?: Record<string, string>): Record<string, string> {
  const allowed = new Set(task.requirements.allowed_block_ids)
  return Object.fromEntries((task.candidate?.blocks ?? [])
    .filter(block => allowed.has(block.block_id))
    .map(block => [block.block_id, previous?.[block.block_id] ?? block.text]))
}

function canDiscardRejectedOperation(
  operation: RevisionPendingOperation,
  task: RevisionTaskOutput | null,
  error: unknown,
): boolean {
  if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) return false
  if (error.status === 422 || error.status === 400) return true
  if (error.status !== 409) return false
  if (operation.kind === 'create') return error.code === 'REVISION_IDEMPOTENCY_CONFLICT' || error.code === 'REVISION_SOURCE_CONFLICT'
  if (['REVISION_TASK_CONFLICT', 'REVISION_CHECKPOINT_CONFLICT', 'REVISION_IDEMPOTENCY_CONFLICT'].includes(error.code ?? '')) return true
  const stage = operation.kind === 'review' ? 'awaiting_review'
    : operation.kind === 'clarification' ? 'needs_clarification' : 'created'
  return Boolean(task && task.status === stage
    && task.version === operation.payload.expected_task_version
    && task.checkpoint_id === operation.payload.expected_checkpoint_id)
}

export default function Revisions(props: RevisionProps) {
  return <RevisionWorkspace key={[props.userId, props.teamId, props.briefId].join(':')} {...props} />
}

function RevisionWorkspace({
  briefId, teamId, userId, brandId, platforms, disabled = false, onAdopted, onBusyChange,
}: RevisionProps) {
  const titleId = useId()
  const scope = useMemo(() => ({ userId, teamId, briefId }), [userId, teamId, briefId])
  const storageKey = selectedStorageKey(userId, teamId, briefId)
  const mounted = useRef(true)
  const inFlight = useRef(false)
  const selectionRequest = useRef(0)
  const [platform, setPlatform] = useState(platforms[0] ?? '')
  const [source, setSource] = useState<DraftSnapshot | null>(null)
  const [sourceLoading, setSourceLoading] = useState(true)
  const [sourceError, setSourceError] = useState<string | null>(null)
  const [sourceReload, setSourceReload] = useState(0)
  const [tasks, setTasks] = useState<RevisionTaskOutput[]>([])
  const [task, setTask] = useState<RevisionTaskOutput | null>(null)
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [documents, setDocuments] = useState<BrandDocument[]>([])
  const [documentsError, setDocumentsError] = useState<string | null>(null)
  const [documentsReload, setDocumentsReload] = useState(0)
  const [documentIds, setDocumentIds] = useState<string[]>([])
  const [allowedIds, setAllowedIds] = useState<string[]>([])
  const [goal, setGoal] = useState('')
  const [mustKeep, setMustKeep] = useState('')
  const [brandRules, setBrandRules] = useState('')
  const [limit, setLimit] = useState('')
  const [unit, setUnit] = useState<'unicode_chars' | 'whitespace_words'>('unicode_chars')
  const [maxRevisions, setMaxRevisions] = useState(3)
  const [feedback, setFeedback] = useState('')
  const [clarificationGoal, setClarificationGoal] = useState('')
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [pending, setPending] = useState<RevisionPendingOperation | null>(null)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirmationRequired, setConfirmationRequired] = useState(false)
  const [requiresNewSource, setRequiresNewSource] = useState(false)
  const [permissionDenied, setPermissionDenied] = useState(false)
  const taskId = task?.task_id ?? null
  const taskStatus = task?.status ?? null
  const blocked = disabled || busy !== null || pending !== null || storageError !== null
  const allowed = new Set(task?.requirements.allowed_block_ids ?? [])
  const issues = [...new Set(task?.issues ?? [])]
  const active = busy !== null || pending !== null || LIVE_STATUSES.has(taskStatus ?? '')

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    onBusyChange?.(active)
    return () => { onBusyChange?.(false) }
  }, [active, onBusyChange])

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    void (async () => {
      let restored: RevisionPendingOperation | null = null
      try {
        restored = readPendingRevisionOperation(scope)
        if (!cancelled) {
          setPending(restored)
          if (restored?.kind === 'create') {
            setPlatform(restored.payload.platform)
            setGoal(restored.payload.requirements.goal)
            setMustKeep(restored.payload.requirements.must_keep.join('\n'))
            setBrandRules((restored.payload.brand_rules ?? []).join('\n'))
            setDocumentIds(restored.payload.evidence_document_ids ?? [])
            setAllowedIds(restored.payload.requirements.allowed_block_ids)
            setMaxRevisions(restored.payload.max_revisions ?? 3)
          }
        }
      } catch (error) {
        if (!cancelled) setStorageError(errorText(error))
      }
      try {
        const history = await listRevisionTasks(briefId, teamId, controller.signal)
        if (cancelled) return
        setTasks(history)
        let selected = pendingTaskId(restored)
        try { selected ||= window.localStorage.getItem(storageKey) } catch { /* optional */ }
        if (!selected && !restored) selected = history[0]?.task_id ?? null
        if (selected) {
          const loaded = history.find(item => item.task_id === selected)
            ?? await getRevisionTask(selected, teamId, controller.signal)
          if (cancelled) return
          if (loaded.brief_id !== briefId) throw new Error('恢复记录不属于当前营销任务，请核对改稿记录。')
          setTask(loaded)
          setPlatform(loaded.platform)
          setEdits(editableTexts(loaded))
          rememberSelection(storageKey, loaded.task_id)
        }
      } catch (error) {
        if (!cancelled) {
          setHistoryError(errorText(error))
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) setPermissionDenied(true)
        }
      } finally {
        if (!cancelled) setHistoryLoading(false)
      }
    })()
    return () => { cancelled = true; controller.abort() }
  }, [briefId, teamId, scope, storageKey])

  useEffect(() => {
    if (taskId || !platform || permissionDenied) return
    let cancelled = false
    const controller = new AbortController()
    void readSource(briefId, platform, teamId, controller.signal)
      .then(value => {
        if (value.source_brief_id !== briefId) throw new Error('读取的原稿不属于当前营销任务。')
        if (!cancelled) setSource(value)
      })
      .catch(error => {
        if (!cancelled) {
          setSourceError(errorText(error))
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) setPermissionDenied(true)
        }
      })
      .finally(() => { if (!cancelled) setSourceLoading(false) })
    return () => { cancelled = true; controller.abort() }
  }, [briefId, teamId, platform, sourceReload, taskId, permissionDenied])

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    void listRevisionEvidenceDocuments(brandId, teamId, controller.signal)
      .then(value => { if (!cancelled) { setDocuments(value); setDocumentsError(null) } })
      .catch(error => { if (!cancelled) setDocumentsError(errorText(error)) })
    return () => { cancelled = true; controller.abort() }
  }, [brandId, teamId, documentsReload])

  function applyTask(next: RevisionTaskOutput, preserveEdits = false) {
    if (!mounted.current) return
    if (next.brief_id !== briefId) throw new Error('改稿结果不属于当前营销任务。')
    setTask(current => current?.task_id === next.task_id && current.version > next.version ? current : next)
    setTasks(current => [next, ...current.filter(item => item.task_id !== next.task_id)])
    setPlatform(next.platform)
    setEdits(current => editableTexts(next, preserveEdits ? current : undefined))
    rememberSelection(storageKey, next.task_id)
  }

  useEffect(() => {
    if (!taskId || (!LIVE_STATUSES.has(taskStatus ?? '') && !busy)) return
    let cancelled = false
    let timer = 0
    const controller = new AbortController()
    const poll = async () => {
      try {
        const latest = await getRevisionTask(taskId, teamId, controller.signal)
        if (cancelled) return
        setTask(current => current?.task_id === latest.task_id && current.version <= latest.version ? latest : current)
        if (LIVE_STATUSES.has(latest.status)) timer = window.setTimeout(() => void poll(), 2000)
      } catch {
        // The foreground command retains its durable retry record.
      }
    }
    timer = window.setTimeout(() => void poll(), 2000)
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer) }
  }, [taskId, taskStatus, busy, teamId])

  async function refreshTask() {
    if (!task) return
    setActionError(null)
    try {
      const next = await getRevisionTask(task.task_id, teamId)
      if (!mounted.current) return
      if (next.candidate?.candidate_id !== task.candidate?.candidate_id) setConfirmationRequired(true)
      applyTask(next, true)
    } catch (error) {
      if (mounted.current) setActionError(errorText(error))
    }
  }

  async function chooseTask(id: string) {
    if (blocked) return
    if (!id) {
      selectionRequest.current += 1
      setTask(null)
      setSource(null)
      setSourceLoading(true)
      setSourceError(null)
      setAllowedIds([])
      setPlatform(current => platforms.includes(current) ? current : (platforms[0] ?? ''))
      setFeedback('')
      setClarificationGoal('')
      setEdits({})
      setActionError(null)
      setNotice(null)
      setConfirmationRequired(false)
      setRequiresNewSource(false)
      setSourceReload(value => value + 1)
      rememberSelection(storageKey, null)
      return
    }
    const request = ++selectionRequest.current
    setBusy('读取任务…')
    setActionError(null)
    try {
      const next = await getRevisionTask(id, teamId)
      if (!mounted.current || request !== selectionRequest.current) return
      applyTask(next)
      setFeedback('')
      setClarificationGoal('')
      setConfirmationRequired(false)
      setRequiresNewSource(false)
      setPermissionDenied(false)
      setNotice(null)
    } catch (error) {
      if (mounted.current) setActionError(errorText(error))
    } finally {
      if (mounted.current && request === selectionRequest.current) setBusy(null)
    }
  }

  async function dispatch(operation: RevisionPendingOperation): Promise<RevisionTaskOutput> {
    if (operation.kind === 'create') return createRevisionTask(briefId, operation.payload, teamId)
    if (operation.kind === 'run') return runRevisionTask(operation.task_id, operation.payload, teamId)
    if (operation.kind === 'clarification') return clarifyRevisionTask(operation.task_id, operation.payload, teamId)
    return reviewRevisionTask(operation.task_id, operation.payload, teamId)
  }

  async function perform(operation: RevisionPendingOperation) {
    if (inFlight.current || disabled || storageError) return
    inFlight.current = true
    setActionError(null)
    setNotice(null)
    setRequiresNewSource(false)
    let current = operation
    try {
      for (;;) {
        savePendingRevisionOperation(scope, current)
        if (!mounted.current) return
        setPending(current)
        setBusy(current.kind === 'review' ? '正在审核并校验最终稿…'
          : current.kind === 'clarification' ? '正在补充资料并继续改稿…'
            : current.kind === 'create' ? '正在创建改稿任务…' : '正在改稿与检查…')
        const next = await dispatch(current)
        let cacheFailure: string | null = null
        try {
          clearPendingRevisionOperation(scope, current.payload.idempotency_key)
        } catch (error) {
          cacheFailure = errorText(error)
        }
        if (!mounted.current) return
        applyTask(next)
        if (cacheFailure) setStorageError(cacheFailure)
        else setPending(null)
        setConfirmationRequired(false)
        setRequiresNewSource(false)
        setFeedback('')
        setClarificationGoal('')
        if (current.kind === 'create') {
          if (cacheFailure) break
          current = {
            kind: 'run', task_id: next.task_id,
            payload: revisionCommand(next, newRevisionKey()),
          }
          continue
        }
        if (next.status === 'accepted') {
          setNotice('指定段落已采纳，完整营销稿已返回整稿审核。')
          try { await onAdopted() } catch (error) {
            if (mounted.current) setNotice('改稿已采纳；营销稿刷新失败，请刷新页面查看。' + errorText(error))
          }
        }
        break
      }
    } catch (error) {
      let fresh: RevisionTaskOutput | null = null
      const id = pendingTaskId(current)
      if (id) {
        try { fresh = await getRevisionTask(id, teamId) } catch { /* retain original error and pending intent */ }
      }
      if (canDiscardRejectedOperation(current, fresh, error)) {
        try {
          clearPendingRevisionOperation(scope, current.payload.idempotency_key)
          if (mounted.current) setPending(null)
        } catch (cacheError) {
          if (mounted.current) setStorageError(errorText(cacheError))
        }
      }
      if (mounted.current) {
        if (fresh) applyTask(fresh, true)
        setActionError(errorText(error))
        if (error instanceof ApiError && error.status === 409) {
          const stageUnchanged = current.kind !== 'create' && fresh
            && fresh.version === current.payload.expected_task_version
            && fresh.checkpoint_id === current.payload.expected_checkpoint_id
            && ['awaiting_review', 'needs_clarification', 'created'].includes(fresh.status)
          setConfirmationRequired(!stageUnchanged)
          if (current.kind === 'create' && error.code === 'REVISION_SOURCE_CONFLICT') reloadSource()
        }
        if (error instanceof ApiError && ['REVISION_SOURCE_CONFLICT', 'REVISION_EVIDENCE_CONFLICT'].includes(error.code ?? '')) setRequiresNewSource(true)
        if (error instanceof ApiError && ([401, 403].includes(error.status) || (error.status === 404 && error.code !== 'REVISION_EVIDENCE_NOT_FOUND'))) setPermissionDenied(true)
      }
    } finally {
      inFlight.current = false
      if (mounted.current) setBusy(null)
    }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (blocked || !source) return
    if (!allowedIds.length) { setActionError('请至少选择一个允许修改的段落。'); return }
    if (!goal.trim()) { setActionError('请填写修改目标。'); return }
    const maximum = limit ? Number(limit) : null
    if (maximum !== null && (!Number.isInteger(maximum) || maximum < 1)) {
      setActionError('字数上限必须是大于 0 的整数。')
      return
    }
    const payload: RevisionCreateRequest = {
      platform, expected_draft_id: source.draft_id, expected_draft_version: source.version,
      requirements: {
        goal: goal.trim(), allowed_block_ids: allowedIds, must_keep: lines(mustKeep),
        length_limits: maximum === null ? [] : allowedIds.map(block_id => ({ block_id, unit, maximum })),
      },
      brand_rules: lines(brandRules), evidence_document_ids: documentIds,
      max_revisions: maxRevisions, idempotency_key: newRevisionKey(),
    }
    void perform({ kind: 'create', payload })
  }

  function submitClarification(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (blocked || !task) return
    if (!feedback.trim()) { setActionError('请填写补充信息。'); return }
    void perform({
      kind: 'clarification', task_id: task.task_id,
      payload: {
        ...revisionCommand(task, newRevisionKey()), feedback: feedback.trim(),
        ...(clarificationGoal.trim() ? { goal: clarificationGoal.trim() } : {}),
        evidence_document_ids: documentIds,
      },
    })
  }

  function review(action: 'approved' | 'rejected') {
    if (blocked || !task || confirmationRequired) return
    if (action === 'approved' && requiresNewSource) return
    if (action === 'rejected' && !feedback.trim()) {
      setActionError('拒绝改稿时请填写原因。')
      return
    }
    try {
      const blocks = action === 'approved' ? buildRevisionReviewBlocks(task, edits) : undefined
      const changed = blocks && blocks.some((block, index) => block.text !== task.candidate?.blocks[index]?.text)
      void perform({
        kind: 'review', task_id: task.task_id,
        payload: {
          ...revisionCommand(task, newRevisionKey()), action,
          ...(feedback.trim() ? { feedback: feedback.trim() } : {}),
          ...(changed ? { blocks } : {}),
        },
      })
    } catch (error) {
      setActionError(errorText(error))
    }
  }

  async function downloadSample() {
    if (!task || busy) return
    setBusy('正在导出样本…')
    setActionError(null)
    try {
      const sample = await exportRevisionSample(task.task_id, teamId)
      if (!mounted.current) return
      const url = URL.createObjectURL(new Blob([JSON.stringify(sample, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = 'revision-sample-' + task.task_id + '.json'
      link.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      if (mounted.current) setActionError(errorText(error))
    } finally {
      if (mounted.current) setBusy(null)
    }
  }

  function toggleDocument(id: string) {
    setDocumentIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id])
  }

  function reloadSource() {
    setSource(null)
    setSourceLoading(true)
    setSourceError(null)
    setAllowedIds([])
    setSourceReload(value => value + 1)
  }

  function changePlatform(value: string) {
    setPlatform(value)
    setSource(null)
    setSourceLoading(true)
    setSourceError(null)
    setAllowedIds([])
  }

  function newTaskFromCurrentSource() {
    if (!task || busy || !requiresNewSource || (pending && task.status !== 'error')) return
    try {
      if (pending) clearPendingRevisionOperation(scope, pending.payload.idempotency_key)
      setPending(null)
      setGoal(task.requirements.goal)
      setMustKeep(task.requirements.must_keep.join('\n'))
      setBrandRules((task.review_context?.brand_rules ?? []).join('\n'))
      setDocumentIds([])
      setTask(null)
      setSource(null)
      setSourceLoading(true)
      setSourceError(null)
      setAllowedIds([])
      setFeedback('')
      setEdits({})
      setConfirmationRequired(false)
      setRequiresNewSource(false)
      setActionError(null)
      setSourceReload(value => value + 1)
      setNotice('旧任务与候选仍保留在改稿记录中。请重新选择最新原稿中的段落。')
      rememberSelection(storageKey, null)
    } catch (error) {
      setStorageError(errorText(error))
    }
  }

  const sourcePicker = (
    <div className="gp-revision__sources">
      <div className="gp-revision__row">
        <span>品牌资料（可选，不选则自动检索）</span>
        <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={busy !== null} onClick={() => setDocumentsReload(value => value + 1)}>刷新资料</button>
      </div>
      {documentsError ? <p className="gp-revision__error" role="alert">{documentsError}</p> : null}
      {!documents.length && !documentsError ? <p className="gp-revision__muted">品牌库暂无资料。上传资料后刷新，再继续任务。</p> : null}
      {documents.map(item => (
        <label key={item.id} className="gp-revision__source-option">
          <input type="checkbox" checked={documentIds.includes(item.id)} disabled={blocked} onChange={() => toggleDocument(item.id)} />
          <span>{item.source} <small>v{item.version}</small></span>
        </label>
      ))}
    </div>
  )

  return (
    <section className="gp-panel gp-revision" aria-labelledby={titleId} aria-busy={busy !== null}>
      <header className="gp-revision__header">
        <div>
          <p className="gp-marketing__card-kicker">Paragraph revision</p>
          <h2 id={titleId} className="gp-panel__title">定向改稿</h2>
        </div>
        {task ? <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={blocked} onClick={() => void chooseTask('')}>新建改稿任务</button> : null}
      </header>
      <p className="gp-panel__meta">选择正文段落并提出修改要求，检查通过后由你审核采纳。</p>
      {historyLoading ? <p className="gp-revision__muted" role="status">正在读取改稿任务…</p> : null}
      {historyError ? <p className="gp-revision__error" role="alert">{historyError}</p> : null}
      {tasks.length ? (
        <label className="gp-field gp-revision__history">改稿记录
          <select value={taskId ?? ''} disabled={blocked} onChange={event => void chooseTask(event.target.value)}>
            <option value="">新建任务</option>
            {tasks.map(item => <option key={item.task_id} value={item.task_id}>{PLATFORM_LABELS[item.platform] ?? item.platform} · {revisionStatusLabel(item.status)} · {new Date(item.created_at).toLocaleString('zh-CN')}</option>)}
          </select>
        </label>
      ) : null}

      {storageError ? <p className="gp-revision__error" role="alert">无法保存或读取重试记录：{storageError}。请恢复浏览器本地存储后再提交。</p> : null}
      {actionError ? <p className="gp-revision__error" role="alert">{actionError}</p> : null}
      {pending ? (
        <div className="gp-revision__pending" role="status">
          <p>{busy ? busy : storageError ? '本地恢复记录无法更新。恢复浏览器存储后可重试确认结果。' : '上次请求的结果尚未确认，请使用原请求重试，避免重复执行。'}</p>
          {!busy ? <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={disabled || storageError !== null} onClick={() => void perform(pending)}>重试上次请求</button> : null}
          {!busy && requiresNewSource && task?.status === 'error' ? <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={disabled || storageError !== null} onClick={newTaskFromCurrentSource}>基于最新原稿新建任务</button> : null}
        </div>
      ) : busy ? <p className="gp-revision__muted" role="status">{busy}</p> : null}
      {notice ? <p className="gp-revision__notice" role="status">{notice}</p> : null}

      {permissionDenied ? (
        <p className="gp-revision__error">当前账户无法继续访问此改稿任务，请重新选择有权限的营销任务。</p>
      ) : task ? (
        <div className="gp-revision__task">
          <div className="gp-revision__row gp-revision__summary">
            <span className={'gp-badge' + (['error', 'budget_exhausted', 'needs_clarification'].includes(task.status) ? ' gp-badge--warn' : '')}>{revisionStatusLabel(task.status)}</span>
            <span>{PLATFORM_LABELS[task.platform] ?? task.platform} · 原稿 v{task.draft.version} · 自动修正 {task.revision_attempts}/{task.max_revisions}</span>
            <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={busy !== null} onClick={() => void refreshTask()}>刷新任务</button>
          </div>
          <p className="gp-revision__goal">{task.requirements.goal}</p>
          {issues.length ? <ul className="gp-revision__issues">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul> : task.status === 'awaiting_review' ? <p className="gp-revision__notice">本轮检查通过，请对照原稿审核候选。</p> : null}
          {task.status === 'accepted' ? <p className="gp-revision__notice">指定段落已采纳为正文 v{task.adopted_version}，完整营销稿仍需整稿审核。</p> : null}
          {['budget_exhausted', 'error', 'rejected'].includes(task.status) ? <p className="gp-revision__muted">任务与检查记录已保留。需要再次修改时，可调整要求并新建任务。</p> : null}
          {requiresNewSource && !pending ? (
            <div className="gp-revision__pending">
              <p>原稿或品牌资料版本已变化，本次任务不能继续采纳。旧任务及已有结果会保留，请使用最新原稿重新选择段落。</p>
              <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={disabled || busy !== null || storageError !== null} onClick={newTaskFromCurrentSource}>基于最新原稿新建任务</button>
            </div>
          ) : null}
          {task.candidate ? (
            <div className="gp-revision__comparison" aria-label="原稿与候选对照">
              <div className="gp-revision__comparison-heading"><span>原稿</span><span>{task.status === 'awaiting_review' ? '候选稿 · 可编辑选定段落' : '候选稿'}</span></div>
              {task.draft.blocks.map((block, index) => {
                const candidate = task.candidate?.blocks.find(item => item.block_id === block.block_id)
                const editable = allowed.has(block.block_id)
                const limitForBlock = task.requirements.length_limits.filter(item => item.block_id === block.block_id)
                return (
                  <div className={'gp-revision__comparison-row' + (editable ? ' is-editable' : '')} key={block.block_id}>
                    <div className="gp-revision__original"><strong>第 {index + 1} 段 <small>{editable ? '允许修改' : '保持原样'}</small></strong><p>{block.text}</p></div>
                    <div className="gp-revision__candidate">
                      {editable && task.status === 'awaiting_review' ? (
                        <label className="gp-field">第 {index + 1} 段最终稿
                          <textarea aria-label={'第 ' + (index + 1) + ' 段最终稿'} rows={Math.max(3, Math.min(8, (edits[block.block_id] ?? candidate?.text ?? '').split('\n').length + 1))} value={edits[block.block_id] ?? candidate?.text ?? ''} disabled={blocked} onChange={event => setEdits(current => ({ ...current, [block.block_id]: event.target.value }))} />
                        </label>
                      ) : <p>{candidate?.text ?? '该段落未出现在候选中'}</p>}
                      {limitForBlock.map(item => <small key={item.unit}>{countRevisionText(edits[block.block_id] ?? candidate?.text ?? '', item.unit)} / {item.maximum} {item.unit === 'unicode_chars' ? '字符' : '词'}</small>)}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : null}

          {confirmationRequired && !pending ? (
            <div className="gp-revision__pending">
              <p>任务已变化，你的编辑仍保留。请对照最新候选；原稿版本变化时需要新建任务。</p>
              {!requiresNewSource ? <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={busy !== null} onClick={() => setConfirmationRequired(false)}>已核对最新任务</button> : null}
            </div>
          ) : null}
          {task.status === 'needs_clarification' ? (
            <form className="gp-revision__form" onSubmit={submitClarification}>
              <label className="gp-field">补充信息<textarea required value={feedback} disabled={blocked} onChange={event => setFeedback(event.target.value)} placeholder="回答检查问题；新增产品事实请先补充品牌资料。" /></label>
              <details className="gp-revision__advanced"><summary>更新目标或指定资料</summary>
                <label className="gp-field">新的修改目标（可选）<textarea value={clarificationGoal} disabled={blocked} onChange={event => setClarificationGoal(event.target.value)} /></label>
                {sourcePicker}
              </details>
              <button type="submit" className="gp-btn gp-btn--primary" disabled={blocked || !feedback.trim()}>补充并继续</button>
            </form>
          ) : null}
          {task.status === 'awaiting_review' ? (
            <div className="gp-revision__form">
              <label className="gp-field">审核反馈<textarea value={feedback} disabled={blocked} onChange={event => setFeedback(event.target.value)} placeholder="采纳时可留空；拒绝时请说明原因。" /></label>
              <div className="gp-revision__actions">
                <button type="button" className="gp-btn gp-btn--primary" disabled={blocked || confirmationRequired || requiresNewSource || task.validation?.status !== 'passed'} onClick={() => review('approved')}>采纳最终稿</button>
                <button type="button" className="gp-btn gp-btn--danger" disabled={blocked || confirmationRequired} onClick={() => review('rejected')}>拒绝本次改稿</button>
              </div>
            </div>
          ) : null}
          {task.status === 'created' && !pending ? <button type="button" className="gp-btn gp-btn--primary" disabled={blocked} onClick={() => void perform({ kind: 'run', task_id: task.task_id, payload: revisionCommand(task, newRevisionKey()) })}>开始改稿</button> : null}
          {task.status === 'accepted' ? <button type="button" className="gp-btn gp-btn--ghost" disabled={busy !== null} onClick={() => void downloadSample()}>导出采纳样本</button> : null}
          {task.review_context?.evidence.length ? (
            <details className="gp-revision__evidence"><summary>查看品牌依据 · {task.review_context.evidence.length} 条</summary>
              {task.review_context.evidence.map(item => <article key={item.evidence_id}><strong>{item.source} · v{item.document_version}</strong><blockquote>{item.quote}</blockquote></article>)}
            </details>
          ) : null}
        </div>
      ) : (
        <form className="gp-revision__form" onSubmit={create}>
          <label className="gp-field">改稿平台<select value={platform} disabled={blocked || !platforms.length} onChange={event => changePlatform(event.target.value)}>{platforms.map(item => <option key={item} value={item}>{PLATFORM_LABELS[item] ?? item}</option>)}</select></label>
          {sourceLoading ? <p className="gp-revision__muted" role="status">正在读取原稿段落…</p> : null}
          {sourceError ? <div className="gp-revision__row"><p className="gp-revision__error" role="alert">{sourceError}</p><button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" disabled={blocked} onClick={reloadSource}>重新读取原稿</button></div> : null}
          {source ? (
            <fieldset className="gp-revision__paragraphs" disabled={blocked}>
              <legend>允许修改的段落 <small>正文 v{source.version}</small></legend>
              {source.blocks.map((block, index) => <label key={block.block_id} className={'gp-revision__paragraph' + (allowedIds.includes(block.block_id) ? ' is-selected' : '')}>
                <input type="checkbox" checked={allowedIds.includes(block.block_id)} onChange={() => setAllowedIds(current => current.includes(block.block_id) ? current.filter(id => id !== block.block_id) : [...current, block.block_id])} />
                <span><strong>第 {index + 1} 段</strong><span className="gp-revision__paragraph-text">{block.text}</span></span>
              </label>)}
            </fieldset>
          ) : null}
          <label className="gp-field">修改目标<textarea required value={goal} disabled={blocked} onChange={event => setGoal(event.target.value)} placeholder="例如：只精简选定段落，突出 65W 功率，表达自然。" /></label>
          <label className="gp-field">必须保留的原文片段<textarea value={mustKeep} disabled={blocked} onChange={event => setMustKeep(event.target.value)} placeholder={'每行一项，例如：\n65W\n30 天退货'} /></label>
          <details className="gp-revision__advanced"><summary>字数、品牌规则与资料</summary>
            <div className="gp-revision__fields">
              <label className="gp-field">每个选中段落的长度上限<input type="number" min="1" step="1" value={limit} disabled={blocked} onChange={event => setLimit(event.target.value)} placeholder="不限" /></label>
              <label className="gp-field">计数方式<select value={unit} disabled={blocked} onChange={event => setUnit(event.target.value as typeof unit)}><option value="unicode_chars">字符（含空格与标点）</option><option value="whitespace_words">按空白分隔的词</option></select></label>
              <label className="gp-field">最多自动修正次数<select value={maxRevisions} disabled={blocked} onChange={event => setMaxRevisions(Number(event.target.value))}>{[0, 1, 2, 3].map(value => <option key={value} value={value}>{value} 次</option>)}</select></label>
            </div>
            <label className="gp-field">品牌表达规则<textarea value={brandRules} disabled={blocked} onChange={event => setBrandRules(event.target.value)} placeholder="每行一条，例如：避免夸张承诺。" /></label>
            {sourcePicker}
          </details>
          <div className="gp-revision__actions"><button type="submit" className="gp-btn gp-btn--primary" disabled={blocked || !source || !allowedIds.length || !goal.trim()}>创建并开始改稿</button><span className="gp-revision__muted">仅修改选定的正文段落。</span></div>
        </form>
      )}
    </section>
  )
}
