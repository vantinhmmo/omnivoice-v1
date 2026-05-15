import { ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import { cancelJob, createLongJob, downloadUrl, getHealth, listJobs, logUrl } from './api';
import type { Health, LongJob } from './types';

type QueueRow = {
  id: string;
  text: string;
  chars: number;
  status: string;
  pause?: number;
  duration?: number;
};

type SettingsState = Record<string, string | boolean>;

type VoicePreset = {
  id: string;
  name: string;
  settings: SettingsState;
  createdAt: string;
};

type ToastState = { message: string; type: 'ok' | 'error' } | null;

const SETTINGS_LS_KEY = 'omnivoice.longtext.settings.v1';
const VOICE_PRESETS_LS_KEY = 'omnivoice.longtext.voice-presets.v1';
const CLIENT_USER_ID_LS_KEY = 'omnivoice.longtext.client-user-id.v1';

const DEFAULT_SETTINGS: SettingsState = {
  model: 'k2-fsa/OmniVoice',
  language: 'vi',
  output_format: 'wav',
  ref_text: '',
  num_step: '32',
  max_chars: '1200',
  min_chars: '120',
  guidance_scale: '2.0',
  speed: '1.0',
  pause_scale: '1.0',
  comma_pause: '0.18',
  sentence_pause: '0.45',
  paragraph_pause: '0.75',
  smart_pause: true,
  postprocess_output: true,
};

const PRIMARY_LANGUAGES = ['Auto', 'Vietnamese', 'English', 'Thai', 'Japanese', 'Spanish', 'Indonesian', 'French'] as const;
const ACTIVE_STATUSES = ['running', 'queued', 'pending'] as const;
const TERMINAL_STATUSES = ['cancelled', 'stopped', 'done', 'failed', 'merged'] as const;

function normalizeStatus(status?: string | null): string {
  return String(status ?? '').trim().toLowerCase();
}

function isActiveStatus(status?: string | null): boolean {
  return ACTIVE_STATUSES.includes(normalizeStatus(status) as (typeof ACTIVE_STATUSES)[number]);
}

function isTerminalStatus(status?: string | null): boolean {
  return TERMINAL_STATUSES.includes(normalizeStatus(status) as (typeof TERMINAL_STATUSES)[number]);
}

function hasActiveChunks(job?: LongJob | null): boolean {
  if (!job) return false;
  return (job.manifest?.chunks ?? []).some((chunk) => isActiveStatus(chunk.status));
}

function deriveEffectiveJobStatus(job?: LongJob | null): string {
  if (!job) return '';

  const rawStatus = normalizeStatus(job.status);
  const finalStatus = normalizeStatus(job.manifest?.summary?.final_status);

  if (finalStatus && isTerminalStatus(finalStatus)) {
    return finalStatus;
  }

  if (job.output_exists && !hasActiveChunks(job) && isActiveStatus(rawStatus)) {
    return 'done';
  }

  return rawStatus;
}

function normalizeLanguageValue(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return 'Auto';

  const lower = raw.toLowerCase();
  const codeMap: Record<string, string> = {
    auto: 'Auto',
    vi: 'Vietnamese',
    en: 'English',
    th: 'Thai',
    ja: 'Japanese',
    es: 'Spanish',
    id: 'Indonesian',
    fr: 'French',
  };

  if (codeMap[lower]) return codeMap[lower];
  const exact = PRIMARY_LANGUAGES.find((name) => name.toLowerCase() === lower);
  return exact ?? raw;
}

function safeLoadSettings(): SettingsState {
  try {
    const raw = localStorage.getItem(SETTINGS_LS_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<SettingsState>) : {};
    const merged = { ...DEFAULT_SETTINGS, ...parsed } as SettingsState;
    merged.language = normalizeLanguageValue(merged.language);
    return merged;
  } catch {
    const fallback = { ...DEFAULT_SETTINGS } as SettingsState;
    fallback.language = normalizeLanguageValue(fallback.language);
    return fallback;
  }
}

function safeLoadPresets(): VoicePreset[] {
  try {
    const raw = localStorage.getItem(VOICE_PRESETS_LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as VoicePreset[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function safeLoadClientUserId(): string {
  try {
    const existing = localStorage.getItem(CLIENT_USER_ID_LS_KEY);
    if (existing && existing.trim()) return existing.trim();
    const generated = crypto.randomUUID();
    localStorage.setItem(CLIENT_USER_ID_LS_KEY, generated);
    return generated;
  } catch {
    return crypto.randomUUID();
  }
}

function pct(job?: LongJob | null): number {
  const summary = job?.manifest?.summary;
  const total = summary?.total_chunks ?? 0;
  const done = summary?.completed_chunks ?? 0;
  return total > 0 ? Math.round((done / total) * 100) : 0;
}

function fmtSeconds(v?: number | null): string {
  if (!v || v <= 0) return '0s';
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  const s = Math.round(v % 60);
  return [h ? `${h}h` : '', m ? `${m}m` : '', `${s}s`].filter(Boolean).join(' ');
}

function splitPreview(text: string, maxChars: number): QueueRow[] {
  const normalized = text.replace(/\r\n/g, '\n').trim();
  if (!normalized) return [];
  const parts = normalized
    .split(/(?<=[,.!?;:。！？；：，])\s+|\n\s*\n/g)
    .map((p) => p.trim())
    .filter(Boolean);

  const rows: string[] = [];
  let current = '';

  for (const part of parts) {
    if (!current) current = part;
    else if ((current + ' ' + part).length <= maxChars) current += ` ${part}`;
    else {
      rows.push(current);
      current = part;
    }
  }

  if (current) rows.push(current);

  return rows.map((chunkText, idx) => ({
    id: `draft-${idx}`,
    text: chunkText,
    chars: chunkText.length,
    status: 'queued',
  }));
}

function jobRows(job?: LongJob | null): QueueRow[] {
  return (job?.manifest?.chunks ?? []).map((chunk) => ({
    id: String(chunk.idx),
    text: chunk.text,
    chars: chunk.chars,
    status: chunk.status,
    pause: chunk.pause_after,
    duration: chunk.duration,
  }));
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    queued: 'Chờ',
    pending: 'Chờ',
    running: 'Đang chạy',
    done: 'Xong',
    failed: 'Lỗi',
    cancelled: 'Đã hủy',
    stopped: 'Dừng',
    merged: 'Đã ghép',
  };
  return map[status] ?? status;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{statusLabel(status)}</span>;
}

function Toast({ toast }: { toast: ToastState }) {
  if (!toast) return null;
  return <div className={`toast toast-${toast.type}`}>{toast.message}</div>;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<LongJob[]>([]);
  const [scriptText, setScriptText] = useState('');
  const [scriptFileName, setScriptFileName] = useState('');
  const [textModalOpen, setTextModalOpen] = useState(false);
  const [modalDraft, setModalDraft] = useState('');
  const [viewMode, setViewMode] = useState<'draft' | 'job'>('draft');
  const [busy, setBusy] = useState(false);
  const [submitUiLocked, setSubmitUiLocked] = useState(false);
  const refAudioRef = useRef<HTMLInputElement | null>(null);
  const submitLockRef = useRef(false);
  const [refAudioPreviewUrl, setRefAudioPreviewUrl] = useState('');

  const [settings, setSettingsState] = useState<SettingsState>(() => safeLoadSettings());
  const [voicePresets, setVoicePresets] = useState<VoicePreset[]>(() => safeLoadPresets());
  const [selectedPresetId, setSelectedPresetId] = useState('');
  const [voiceName, setVoiceName] = useState('');
  const [toast, setToast] = useState<ToastState>(null);

  const [clientUserId] = useState<string>(() => safeLoadClientUserId());

  const latestJob = jobs[0] ?? null;
  const draftRows = useMemo(() => splitPreview(scriptText, Number(settings.max_chars) || 1200), [scriptText, settings.max_chars]);
  const latestJobRows = useMemo(() => (latestJob ? jobRows(latestJob) : []), [latestJob]);
  const rows = viewMode === 'job' ? (latestJobRows.length ? latestJobRows : draftRows) : draftRows;

  const latestJobStatus = useMemo(() => deriveEffectiveJobStatus(latestJob), [latestJob]);
  const runningJobs = useMemo(() => jobs.some((j) => isActiveStatus(deriveEffectiveJobStatus(j))), [jobs]);
  const latestJobHasActiveChunks = useMemo(() => {
    if (!latestJob) return false;
    if (isTerminalStatus(latestJobStatus)) return false;
    return hasActiveChunks(latestJob);
  }, [latestJob, latestJobStatus]);
  const summary = latestJob?.manifest?.summary;
  const progress = pct(latestJob);
  const activeJob = useMemo(() => jobs.find((j) => isActiveStatus(deriveEffectiveJobStatus(j))) ?? null, [jobs]);
  const hasAnyActiveJobs = runningJobs || latestJobHasActiveChunks || !!activeJob;
  const creatingJob = busy || hasAnyActiveJobs;
  const canStopJob = !!latestJob && isActiveStatus(latestJobStatus) && !latestJob.output_exists;

  const debugEnabled = false;

  function flash(message: string, type: 'ok' | 'error' = 'ok') {
    setToast({ message, type });
    window.setTimeout(() => setToast(null), 2200);
  }

  function setSettings(patch: Partial<SettingsState>) {
    setSettingsState((current) => ({ ...current, ...patch }) as SettingsState);
  }

  function saveSettings() {
    try {
      localStorage.setItem(SETTINGS_LS_KEY, JSON.stringify(settings));
      flash('Đã lưu cài đặt.', 'ok');
    } catch {
      flash('Không lưu được cài đặt.', 'error');
    }
  }

  function resetSettings() {
    setSettingsState({ ...DEFAULT_SETTINGS });
    flash('Đã đặt lại cài đặt mặc định.', 'ok');
  }

  function saveVoicePreset() {
    const name = voiceName.trim();
    if (!name) {
      flash('Vui lòng nhập tên giọng trước khi lưu.', 'error');
      return;
    }
    const preset: VoicePreset = {
      id: crypto.randomUUID(),
      name,
      settings: { ...settings },
      createdAt: new Date().toISOString(),
    };
    const next = [preset, ...voicePresets].slice(0, 40);
    setVoicePresets(next);
    setSelectedPresetId(preset.id);
    setVoiceName('');
    localStorage.setItem(VOICE_PRESETS_LS_KEY, JSON.stringify(next));
    flash('Đã lưu giọng.', 'ok');
  }

  function loadVoicePreset() {
    if (!selectedPresetId) {
      flash('Hãy chọn một giọng đã lưu.', 'error');
      return;
    }
    const preset = voicePresets.find((p) => p.id === selectedPresetId);
    if (!preset) {
      flash('Không tìm thấy giọng đã lưu.', 'error');
      return;
    }
    setSettingsState({ ...DEFAULT_SETTINGS, ...preset.settings } as SettingsState);
    flash(`Đã nạp giọng: ${preset.name}`, 'ok');
  }

  function deleteVoicePreset() {
    if (!selectedPresetId) {
      flash('Hãy chọn một giọng để xóa.', 'error');
      return;
    }
    const next = voicePresets.filter((p) => p.id !== selectedPresetId);
    setVoicePresets(next);
    setSelectedPresetId('');
    localStorage.setItem(VOICE_PRESETS_LS_KEY, JSON.stringify(next));
    flash('Đã xóa giọng đã lưu.', 'ok');
  }

  async function refreshJobs() {
    try {
      const next = await listJobs(clientUserId);

      if (debugEnabled) {
        console.debug('[UI][refreshJobs] fetched', {
          count: next.length,
          head: next[0]
            ? {
                id: next[0].id,
                status: next[0].status,
                chunkStatuses: (next[0].manifest?.chunks ?? []).map((c) => c.status),
              }
            : null,
        });
      }
      setJobs(next);
      if (!next.length) setViewMode('draft');
    } catch (err) {
      console.error(err);
      if (debugEnabled) console.debug('[UI][refreshJobs] error', err);
    }
  }

  function ensureCanLoadScript(): boolean {
    return true;
  }

  async function onScriptFile(e: ChangeEvent<HTMLInputElement>) {
    if (!ensureCanLoadScript()) {
      e.target.value = '';
      return;
    }
    const file = e.target.files?.[0];
    if (!file) return;
    setScriptFileName(file.name);
    setScriptText(await file.text());
    setViewMode('draft');
    flash(`Đã nạp file ${file.name}`, 'ok');
  }

  function onRefAudioFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) {
      setRefAudioPreviewUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return '';
      });
      return;
    }

    const nextUrl = URL.createObjectURL(file);
    setRefAudioPreviewUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return nextUrl;
    });
    flash(`Đã nạp reference audio: ${file.name}`, 'ok');
  }

  function openTextModal() {
    if (!ensureCanLoadScript()) return;
    setModalDraft(scriptText);
    setTextModalOpen(true);
  }

  function applyTextModal() {
    if (!ensureCanLoadScript()) return;
    setScriptText(modalDraft);
    setScriptFileName('Nhập thủ công');
    setTextModalOpen(false);
    setViewMode('draft');
    flash('Đã cập nhật nội dung văn bản.', 'ok');
  }

  async function submitJob() {
    if (debugEnabled) {
      console.debug('[UI][submitJob] click', {
        submitLockRef: submitLockRef.current,
        busy,
        submitUiLocked,
        runningJobs,
        latestJobHasActiveChunks,
        activeJobId: activeJob?.id ?? null,
        latestJobStatus,
        creatingJob,
        canStopJob,
      });
    }
    if (submitLockRef.current || busy || submitUiLocked) {
      flash('Yêu cầu tạo job đang được xử lý, vui lòng chờ.', 'error');
      return;
    }
    if (!scriptText.trim()) {
      openTextModal();
      return;
    }

    submitLockRef.current = true;
    setSubmitUiLocked(true);
    setBusy(true);
    try {
      const form = new FormData();
      form.set('script_text', scriptText);
      form.set('user_id', clientUserId);
      for (const [key, value] of Object.entries(settings)) {
        form.set(key, String(value));
      }
      const refFile = refAudioRef.current?.files?.[0];
      if (refFile) form.set('ref_audio', refFile);
      const job = await createLongJob(form);
      if (debugEnabled) console.debug('[UI][submitJob] createLongJob response', { id: job.id, status: job.status });
      setJobs((current) => [job, ...current]);
      setViewMode('job');
      await refreshJobs();
      flash(`Đã tạo job ${job.id}`, 'ok');
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setBusy(false);
      submitLockRef.current = false;
      setSubmitUiLocked(false);
    }
  }

  async function stopJob() {
    if (debugEnabled) {
      console.debug('[UI][stopJob] click', {
        latestJobId: latestJob?.id ?? null,
        latestJobStatus,
        canStopJob,
        latestJobHasActiveChunks,
      });
    }
    if (!latestJob || !canStopJob) {
      if (debugEnabled) {
        console.debug('[UI][stopJob] early-return', {
          reason: !latestJob ? 'no-latest-job' : 'canStopJob=false',
          latestJobId: latestJob?.id ?? null,
          latestJobStatus,
          canStopJob,
          latestJobHasActiveChunks,
          runningJobs,
          activeJobId: activeJob?.id ?? null,
        });
      }
      return;
    }
    try {
      const cancelled = await cancelJob(latestJob.id, clientUserId);
      if (debugEnabled) console.debug('[UI][stopJob] cancel response', { id: cancelled.id, status: cancelled.status });
      setJobs((current) => [cancelled, ...current.filter((j) => j.id !== cancelled.id)]);
      await refreshJobs();
      flash(`Đã gửi lệnh dừng job ${latestJob.id}`, 'ok');
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err), 'error');
      if (debugEnabled) console.debug('[UI][stopJob] error', err);
    }
  }

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
    refreshJobs();
  }, []);

  useEffect(() => {
    const intervalMs = runningJobs ? 3000 : latestJob ? 6000 : 12000;
    const timer = setInterval(() => {
      if (runningJobs || latestJob) refreshJobs();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [runningJobs, latestJob, clientUserId]);

  useEffect(() => {
    const timer = setInterval(() => {
      getHealth().then(setHealth).catch(() => setHealth(null));
    }, 45000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    return () => {
      if (refAudioPreviewUrl) URL.revokeObjectURL(refAudioPreviewUrl);
    };
  }, [refAudioPreviewUrl]);

  useEffect(() => {
    if (!debugEnabled) return;
    console.debug('[UI][state]', {
      jobs: jobs.map((j) => ({
        id: j.id,
        status: j.status,
        chunkStatuses: (j.manifest?.chunks ?? []).map((c) => c.status),
      })),
      latestJobId: latestJob?.id ?? null,
      latestJobStatus,
      runningJobs,
      latestJobHasActiveChunks,
      activeJobId: activeJob?.id ?? null,
      busy,
      submitUiLocked,
      creatingJob,
      canStopJob,
    });
  }, [jobs, latestJob, latestJobStatus, runningJobs, latestJobHasActiveChunks, activeJob, busy, submitUiLocked, creatingJob, canStopJob, hasAnyActiveJobs]);

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>OmniVoice Long Text Studio</h1>
          <p>Luồng nhanh: nhập văn bản → tạo âm thanh → tải kết quả.</p>
        </div>
        <div className="health">
          <span className={health?.ok ? 'dot ok' : 'dot'} />
          <div>
            <b>{health?.ok ? 'API online' : 'API offline'}</b>
            <small>
              {health
                ? `${health.device} · ${health.dtype} · Running ${health.running_long_jobs ?? 0}/${health.max_concurrent_long_jobs ?? 0} · Queue ${health.queued_long_jobs ?? 0}`
                : 'Không kết nối'}
            </small>
          </div>
        </div>
      </header>

      <div className="layout">
        <section className="panel main-panel">
          <div className="step-card">
            <div className="step-head">
              <h2>Bước 1: Nguồn văn bản</h2>
              <span>{scriptText.trim().length.toLocaleString()} ký tự</span>
            </div>
            <div className="source-actions">
              <button type="button" className="btn primary" onClick={openTextModal}>Nhập / sửa văn bản</button>
              <label className="btn soft" htmlFor="script-file">Tải file .txt</label>
              <input id="script-file" type="file" accept=".txt,text/plain" className="hidden" onChange={onScriptFile} />
              <label className="source-lang">
                <span>Language</span>
                <select value={normalizeLanguageValue(settings.language)} onChange={(e) => setSettings({ language: e.target.value })}>
                  {PRIMARY_LANGUAGES.map((lang) => (
                    <option key={lang} value={lang}>{lang}</option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="btn ghost"
                disabled={false}
                onClick={() => {
                  if (!ensureCanLoadScript()) return;
                  setScriptText('');
                  setScriptFileName('');
                  setViewMode('draft');
                }}
              >
                Xóa nội dung
              </button>
            </div>
            {scriptText.trim() ? (
              <div className="script-preview">
                <div className="script-meta">
                  <span>{scriptFileName || 'Nhập thủ công'}</span>
                  <span>{draftRows.length.toLocaleString()} chunk dự kiến</span>
                </div>
                <textarea readOnly value={scriptText} rows={5} />
              </div>
            ) : null}
          </div>

          <div className="step-card">
            <div className="step-head">
              <h2>Bước 2: Hàng đợi chunk</h2>
              <div className="inline-tools queue-tools">
                <span className="queue-single">{viewMode === 'draft' ? 'Đang xem chunk văn bản mới' : 'Đang xem tiến độ job'}</span>
                <button type="button" className="btn ghost" onClick={() => { void refreshJobs(); }}>Làm mới</button>
              </div>
            </div>

            <div className="table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Nội dung</th>
                    <th>Ký tự</th>
                    <th>Pause</th>
                    <th>Dur</th>
                    <th>Trạng thái</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="empty">Chưa có chunk. Hãy nhập văn bản ở bước 1.</td>
                    </tr>
                  ) : (
                    rows.map((row, idx) => (
                      <tr key={row.id}>
                        <td>{idx + 1}</td>
                        <td className="chunk" title={row.text}>{row.text}</td>
                        <td>{row.chars}</td>
                        <td>{row.pause != null ? `${row.pause.toFixed(2)}s` : '-'}</td>
                        <td>{row.duration ? fmtSeconds(row.duration) : '-'}</td>
                        <td><StatusPill status={row.status} /></td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="step-card run-card">
            <div className="step-head">
              <h2>Bước 3: Render & theo dõi</h2>
              <span>{progress}%</span>
            </div>

            <div className="progress">
              <div className="progress-bar" style={{ width: `${progress}%` }} />
            </div>

            <div className="run-stats">
              <span>Chunks: {summary?.completed_chunks ?? 0}/{summary?.total_chunks ?? rows.length}</span>
              <span>Thời lượng: {fmtSeconds(summary?.total_audio_duration)}</span>
            </div>

            <div className="run-actions">
              <button
                type="button"
                className="btn primary"
                onClick={submitJob}
                disabled={submitUiLocked || creatingJob}
                aria-busy={creatingJob}
              >
                {creatingJob ? (
                  <>
                    <span className="btn-spinner" aria-hidden="true" />
                    Đang tạo voice...
                  </>
                ) : 'Tạo âm thanh'}
              </button>
              <button type="button" className="btn danger" onClick={stopJob} disabled={!canStopJob}>Dừng job</button>
              {latestJob?.output_exists && (
                <a className="btn success" href={downloadUrl(latestJob.id, clientUserId)}>Tải audio</a>
              )}
              {latestJob?.log_exists && (
                <a className="btn soft" href={logUrl(latestJob.id, clientUserId)} target="_blank" rel="noreferrer">Xem log</a>
              )}
            </div>

            {hasAnyActiveJobs && (
              <div className="pending-box" role="status" aria-live="polite">
                <span className="pending-dot" />
                <span>
                  {busy
                    ? 'Đang tạo job trên server, vui lòng chờ...'
                    : 'Voice đang được render, vui lòng chờ job hoàn tất hoặc bấm Dừng job.'}
                </span>
              </div>
            )}

            {latestJob?.output_exists && (
              <div className="audio-preview">
                <b>Nghe audio kết quả</b>
                <audio controls preload="metadata" src={`${downloadUrl(latestJob.id, clientUserId)}&t=${encodeURIComponent(latestJob.updated_at)}`} />
              </div>
            )}
          </div>
        </section>

        <aside className="panel side-panel">
          <div className="side-head">
            <h2>Cài đặt</h2>
            <div className="inline-tools">
              <button type="button" className="btn success" onClick={saveSettings}>Lưu</button>
              <button type="button" className="btn ghost" onClick={resetSettings}>Đặt lại</button>
            </div>
          </div>

          <details open>
            <summary>Giọng nói</summary>
            <div className="group">
              <label>Reference audio
                <input ref={refAudioRef} type="file" accept="audio/*" onChange={onRefAudioFile} />
              </label>
              {refAudioPreviewUrl && (
                <div className="audio-preview">
                  <b>Nghe thử reference</b>
                  <audio controls preload="metadata" src={refAudioPreviewUrl} />
                </div>
              )}
              <label>Reference text
                <textarea rows={4} value={String(settings.ref_text)} onChange={(e) => setSettings({ ref_text: e.target.value })} />
              </label>
              <label>Tên giọng lưu
                <input value={voiceName} onChange={(e) => setVoiceName(e.target.value)} placeholder="Ví dụ: Nữ miền Nam nhẹ" />
              </label>
              <div className="inline-tools">
                <button type="button" className="btn warn" onClick={saveVoicePreset}>Lưu giọng</button>
                <button type="button" className="btn ghost" onClick={loadVoicePreset}>Nạp giọng</button>
              </div>
              <label>Giọng đã lưu
                <select value={selectedPresetId} onChange={(e) => setSelectedPresetId(e.target.value)}>
                  <option value="">-- Chọn giọng --</option>
                  {voicePresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>{preset.name}</option>
                  ))}
                </select>
              </label>
              <button type="button" className="btn ghost" onClick={deleteVoicePreset}>Xóa giọng đã chọn</button>
            </div>
          </details>

          <details>
            <summary>Mô hình & render (nâng cao)</summary>
            <div className="group grid2">
              <label>Model
                <input value={String(settings.model)} onChange={(e) => setSettings({ model: e.target.value })} />
              </label>
              <label>Steps
                <input type="number" value={String(settings.num_step)} onChange={(e) => setSettings({ num_step: e.target.value })} />
              </label>
              <label>CFG
                <input type="number" step="0.1" value={String(settings.guidance_scale)} onChange={(e) => setSettings({ guidance_scale: e.target.value })} />
              </label>
              <label>Max chars
                <input type="number" value={String(settings.max_chars)} onChange={(e) => setSettings({ max_chars: e.target.value })} />
              </label>
              <label>Min chars
                <input type="number" value={String(settings.min_chars)} onChange={(e) => setSettings({ min_chars: e.target.value })} />
              </label>
              <label>Speed
                <input type="number" step="0.05" value={String(settings.speed)} onChange={(e) => setSettings({ speed: e.target.value })} />
              </label>
              <label>Pause scale
                <input type="number" step="0.05" value={String(settings.pause_scale)} onChange={(e) => setSettings({ pause_scale: e.target.value })} />
              </label>
            </div>
            <div className="group checks">
              <label className="check"><input type="checkbox" checked={Boolean(settings.smart_pause)} onChange={(e) => setSettings({ smart_pause: e.target.checked })} /> Smart pause</label>
              <label className="check"><input type="checkbox" checked={Boolean(settings.postprocess_output)} onChange={(e) => setSettings({ postprocess_output: e.target.checked })} /> Postprocess output</label>
            </div>
          </details>

          <details>
            <summary>Ngắt nghỉ</summary>
            <div className="group grid2">
              <label>Dấu phẩy
                <input type="number" step="0.01" value={String(settings.comma_pause)} onChange={(e) => setSettings({ comma_pause: e.target.value })} />
              </label>
              <label>Dấu chấm
                <input type="number" step="0.01" value={String(settings.sentence_pause)} onChange={(e) => setSettings({ sentence_pause: e.target.value })} />
              </label>
              <label>Đoạn văn
                <input type="number" step="0.01" value={String(settings.paragraph_pause)} onChange={(e) => setSettings({ paragraph_pause: e.target.value })} />
              </label>
            </div>
          </details>
        </aside>
      </div>

      {textModalOpen && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <div className="modal-head">
              <h2>Nhập văn bản dài</h2>
              <button type="button" className="btn ghost" onClick={() => setTextModalOpen(false)}>Đóng</button>
            </div>
            <textarea
              className="modal-textarea"
              value={modalDraft}
              onChange={(e) => setModalDraft(e.target.value)}
              autoFocus
              placeholder="Dán nội dung dài tại đây..."
            />
            <div className="modal-foot">
              <span>{modalDraft.trim().length.toLocaleString()} ký tự</span>
              <span>{splitPreview(modalDraft, Number(settings.max_chars) || 1200).length.toLocaleString()} chunk dự kiến</span>
              <div className="inline-tools">
                <button type="button" className="btn ghost" onClick={() => setModalDraft('')}>Xóa</button>
                <button type="button" className="btn primary" onClick={applyTextModal}>Áp dụng</button>
              </div>
            </div>
          </div>
        </div>
      )}

      <Toast toast={toast} />
    </main>
  );
}
