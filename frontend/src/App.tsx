import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { cancelJob, createLongJob, downloadUrl, getHealth, listJobs, logUrl, shortTts } from './api';
import type { Health, LongJob } from './types';

type Tab = 'short' | 'long' | 'jobs';

function pct(job: LongJob): number {
  const summary = job.manifest?.summary;
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

function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{status}</span>;
}

function Header({ health }: { health: Health | null }) {
  return (
    <header className="hero">
      <div>
        <p className="eyebrow">OmniVoice Studio</p>
        <h1>Voice cloning & long-form narration</h1>
        <p className="subtitle">
          UI React/Vite cho generate ngắn, render long text theo job queue, theo dõi progress và download file cuối.
        </p>
      </div>
      <div className="health-card">
        <span className={health?.ok ? 'dot ok' : 'dot'} />
        <div>
          <b>{health?.ok ? 'API online' : 'API offline'}</b>
          <small>{health ? `${health.device} · ${health.dtype} · ${health.model}` : 'Chưa kết nối backend'}</small>
        </div>
      </div>
    </header>
  );
}

function Tabs({ active, onChange }: { active: Tab; onChange: (t: Tab) => void }) {
  return (
    <nav className="tabs">
      <button className={active === 'short' ? 'active' : ''} onClick={() => onChange('short')}>Generate ngắn</button>
      <button className={active === 'long' ? 'active' : ''} onClick={() => onChange('long')}>Long text job</button>
      <button className={active === 'jobs' ? 'active' : ''} onClick={() => onChange('jobs')}>Jobs / Progress</button>
    </nav>
  );
}

function ShortTtsPanel() {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('Sẵn sàng');
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [mode, setMode] = useState('clone');
  const refAudioRef = useRef<HTMLInputElement | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setStatus('Đang generate...');
    setAudioUrl(null);
    try {
      const form = new FormData(e.currentTarget);
      form.set('mode', mode);
      const refFile = refAudioRef.current?.files?.[0];
      if (refFile) form.set('ref_audio', refFile);
      const blob = await shortTts(form);
      setAudioUrl(URL.createObjectURL(blob));
      setStatus('Hoàn tất');
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel grid" onSubmit={submit}>
      <section className="card span-2">
        <h2>Generate audio ngắn</h2>
        <label>Text cần đọc</label>
        <textarea name="text" required rows={7} placeholder="Nhập nội dung cần chuyển thành giọng nói..." />
        <div className="row">
          <label>
            Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="clone">Clone voice</option>
              <option value="design">Voice design</option>
              <option value="auto">Auto voice</option>
            </select>
          </label>
          <label>
            Language
            <input name="language" placeholder="vi, en, Auto..." />
          </label>
        </div>
      </section>

      <section className="card">
        <h3>Voice</h3>
        <label>Reference audio</label>
        <input ref={refAudioRef} type="file" accept="audio/*" />
        <label>Reference text</label>
        <textarea name="ref_text" rows={3} placeholder="Transcript của reference audio để tránh ASR" />
        <label>Voice design instruct</label>
        <input name="instruct" placeholder="male, British accent" />
      </section>

      <section className="card">
        <h3>Settings</h3>
        <div className="row compact">
          <label>Steps<input name="num_step" type="number" defaultValue={16} min={4} max={64} /></label>
          <label>CFG<input name="guidance_scale" type="number" defaultValue={2.0} step={0.1} /></label>
        </div>
        <div className="row compact">
          <label>Speed<input name="speed" type="number" defaultValue={1.0} step={0.05} /></label>
          <label>Duration<input name="duration" type="number" step={0.1} placeholder="optional" /></label>
        </div>
        <label className="check"><input name="denoise" type="checkbox" defaultChecked /> Denoise</label>
        <label className="check"><input name="preprocess_prompt" type="checkbox" defaultChecked /> Preprocess prompt</label>
        <label className="check"><input name="postprocess_output" type="checkbox" defaultChecked /> Postprocess output</label>
        <button disabled={busy} className="primary">{busy ? 'Đang chạy...' : 'Generate'}</button>
      </section>

      <section className="card span-2">
        <h3>Kết quả</h3>
        <p className="status-text">{status}</p>
        {audioUrl && <audio controls src={audioUrl} className="audio" />}
      </section>
    </form>
  );
}

function LongJobPanel({ onCreated }: { onCreated: () => void }) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('Sẵn sàng');
  const scriptFileRef = useRef<HTMLInputElement | null>(null);
  const refAudioRef = useRef<HTMLInputElement | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setStatus('Đang tạo job...');
    try {
      const form = new FormData(e.currentTarget);
      const scriptFile = scriptFileRef.current?.files?.[0];
      const refFile = refAudioRef.current?.files?.[0];
      if (scriptFile) form.set('script_file', scriptFile);
      if (refFile) form.set('ref_audio', refFile);
      const job = await createLongJob(form);
      setStatus(`Đã tạo job ${job.id}`);
      onCreated();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel grid" onSubmit={submit}>
      <section className="card span-2">
        <h2>Render long text 2-3 giờ</h2>
        <label>Script text</label>
        <textarea name="script_text" rows={10} placeholder="Dán script dài ở đây hoặc upload file .txt bên dưới..." />
        <label>Hoặc upload .txt</label>
        <input ref={scriptFileRef} type="file" accept=".txt,text/plain" />
      </section>

      <section className="card">
        <h3>Voice clone</h3>
        <label>Reference audio 5-15s</label>
        <input ref={refAudioRef} type="file" accept="audio/*" />
        <label>Reference text</label>
        <textarea name="ref_text" rows={4} placeholder="Nên nhập đúng transcript reference audio" />
        <label>Language</label>
        <input name="language" placeholder="vi" defaultValue="vi" />
        <label>Output</label>
        <select name="output_format" defaultValue="wav">
          <option value="wav">WAV</option>
          <option value="mp3">MP3</option>
        </select>
      </section>

      <section className="card">
        <h3>Render settings</h3>
        <div className="row compact">
          <label>Steps<input name="num_step" type="number" defaultValue={16} min={4} max={64} /></label>
          <label>Max chars<input name="max_chars" type="number" defaultValue={1200} /></label>
        </div>
        <div className="row compact">
          <label>Min chars<input name="min_chars" type="number" defaultValue={120} /></label>
          <label>CFG<input name="guidance_scale" type="number" defaultValue={2.0} step={0.1} /></label>
        </div>
        <div className="row compact">
          <label>Speed<input name="speed" type="number" defaultValue={1.0} step={0.05} /></label>
          <label>Pause scale<input name="pause_scale" type="number" defaultValue={1.0} step={0.05} /></label>
        </div>
        <label className="check"><input name="smart_pause" type="checkbox" defaultChecked /> Smart pause theo dấu câu</label>
        <label className="check"><input name="postprocess_output" type="checkbox" defaultChecked /> Postprocess output</label>
        <details>
          <summary>Pause nâng cao</summary>
          <div className="row compact">
            <label>Dấu phẩy<input name="comma_pause" type="number" defaultValue={0.18} step={0.01} /></label>
            <label>Dấu chấm<input name="sentence_pause" type="number" defaultValue={0.45} step={0.01} /></label>
          </div>
          <label>Đoạn văn<input name="paragraph_pause" type="number" defaultValue={0.75} step={0.01} /></label>
        </details>
        <button disabled={busy} className="primary">{busy ? 'Đang tạo...' : 'Submit long job'}</button>
        <p className="status-text">{status}</p>
      </section>
    </form>
  );
}

function JobCard({ job, onRefresh }: { job: LongJob; onRefresh: () => void }) {
  const summary = job.manifest?.summary;
  const progress = pct(job);
  const chunks = job.manifest?.chunks ?? [];
  const latest = [...chunks].reverse().find((c) => c.status === 'done' || c.status === 'failed' || c.status === 'running');

  async function onCancel() {
    await cancelJob(job.id);
    onRefresh();
  }

  return (
    <article className="job-card">
      <div className="job-head">
        <div>
          <h3>Job {job.id}</h3>
          <small>{new Date(job.created_at).toLocaleString()}</small>
        </div>
        <StatusPill status={job.status} />
      </div>
      <div className="progress"><span style={{ width: `${progress}%` }} /></div>
      <div className="job-metrics">
        <span>{summary?.completed_chunks ?? 0}/{summary?.total_chunks ?? 0} chunks</span>
        <span>{progress}%</span>
        <span>Audio {fmtSeconds(summary?.total_audio_duration)}</span>
        <span>Elapsed {fmtSeconds(summary?.total_elapsed)}</span>
        <span>Pause {fmtSeconds(summary?.inserted_pause_duration)}</span>
        <span>RTF {summary?.average_rtf ? summary.average_rtf.toFixed(2) : '-'}</span>
      </div>
      {latest && <p className="latest">Chunk gần nhất: #{latest.idx} · {latest.status} · pause {latest.pause_after ?? 0}s ({latest.pause_reason ?? 'n/a'})</p>}
      <div className="actions">
        <button onClick={onRefresh}>Refresh</button>
        {job.status === 'running' && <button className="danger" onClick={onCancel}>Cancel</button>}
        {job.output_exists && <a className="button" href={downloadUrl(job.id)}>Download</a>}
        {job.log_exists && <a className="button ghost" href={logUrl(job.id)} target="_blank">Log</a>}
      </div>
    </article>
  );
}

function JobsPanel({ jobs, refresh }: { jobs: LongJob[]; refresh: () => void }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Jobs / Progress</h2>
        <button onClick={refresh}>Refresh</button>
      </div>
      {jobs.length === 0 ? <p className="muted">Chưa có job nào.</p> : <div className="jobs">{jobs.map((j) => <JobCard key={j.id} job={j} onRefresh={refresh} />)}</div>}
    </section>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('short');
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<LongJob[]>([]);
  const runningJobs = useMemo(() => jobs.some((j) => j.status === 'running'), [jobs]);

  async function refreshJobs() {
    try {
      setJobs(await listJobs());
    } catch (err) {
      console.error(err);
    }
  }

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
    refreshJobs();
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      getHealth().then(setHealth).catch(() => setHealth(null));
      if (runningJobs || tab === 'jobs') refreshJobs();
    }, 3000);
    return () => clearInterval(timer);
  }, [runningJobs, tab]);

  return (
    <main className="app">
      <Header health={health} />
      <Tabs active={tab} onChange={setTab} />
      {tab === 'short' && <ShortTtsPanel />}
      {tab === 'long' && <LongJobPanel onCreated={() => { refreshJobs(); setTab('jobs'); }} />}
      {tab === 'jobs' && <JobsPanel jobs={jobs} refresh={refreshJobs} />}
    </main>
  );
}
