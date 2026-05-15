import type { Health, LongJob } from './types';

function withUserId(path: string, userId?: string): string {
  if (!userId) return path;
  const params = new URLSearchParams({ user_id: userId });
  return `${path}?${params.toString()}`;
}

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

export async function listJobs(userId?: string): Promise<LongJob[]> {
  const res = await fetch(withUserId('/api/jobs', userId));
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  return data.jobs ?? [];
}

export async function cancelJob(id: string, userId?: string): Promise<LongJob> {
  const url = withUserId(`/api/jobs/${id}/cancel`, userId);
  console.debug('[UI][api.cancelJob] request', { id, url });
  const res = await fetch(url, { method: 'POST' });
  console.debug('[UI][api.cancelJob] response-meta', { id, ok: res.ok, status: res.status });
  if (!res.ok) {
    const message = await res.text();
    console.debug('[UI][api.cancelJob] response-error', { id, message });
    throw new Error(message);
  }
  const data = await res.json();
  console.debug('[UI][api.cancelJob] response-json', { id: data?.id, status: data?.status });
  return data;
}

export function downloadUrl(id: string, userId?: string): string {
  return withUserId(`/api/jobs/${id}/download`, userId);
}

export function logUrl(id: string, userId?: string): string {
  return withUserId(`/api/jobs/${id}/log`, userId);
}
