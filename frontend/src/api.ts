import type { Health, LongJob } from './types';

export async function getHealth(): Promise<Health> {
  const res = await fetch('/api/health');
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === 'string') return data.detail;
    if (typeof data?.detail?.message === 'string') return data.detail.message;
    return JSON.stringify(data);
  } catch {
    return res.text();
  }
}

export async function createLongJob(form: FormData): Promise<LongJob> {
  const res = await fetch('/api/jobs/long', { method: 'POST', body: form });
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

export async function listJobs(): Promise<LongJob[]> {
  const res = await fetch('/api/jobs');
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  return data.jobs ?? [];
}

export async function cancelJob(id: string): Promise<LongJob> {
  const res = await fetch(`/api/jobs/${id}/cancel`, { method: 'POST' });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function downloadUrl(id: string): string {
  return `/api/jobs/${id}/download`;
}

export function logUrl(id: string): string {
  return `/api/jobs/${id}/log`;
}
