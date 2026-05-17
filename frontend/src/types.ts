export type Health = {
  ok: boolean;
  model: string;
  device: string;
  dtype: string;
  cuda: boolean;
  model_loaded: boolean;
  max_concurrent_long_jobs?: number;
  chunk_workers?: number;
  running_long_jobs?: number;
  queued_long_jobs?: number;
  active_long_jobs?: number;
  total_long_jobs?: number;
};

export type JobSummary = {
  total_chunks?: number;
  completed_chunks?: number;
  failed_chunks?: number;
  total_audio_duration?: number;
  total_elapsed?: number;
  average_rtf?: number | null;
  final_status?: string;
  chunk_workers?: number;
  final_output?: string;
  final_size_bytes?: number;
  inserted_pause_duration?: number;
};

export type JobManifest = {
  summary?: JobSummary;
  chunks?: Array<{
    idx: number;
    text: string;
    wav: string;
    status: string;
    chars: number;
    pause_after?: number;
    pause_reason?: string;
    duration?: number;
    elapsed?: number;
    error?: string | null;
  }>;
};

export type LongJob = {
  id: string;
  status: string;
  pid?: number;
  created_at: string;
  updated_at: string;
  output?: string;
  output_exists?: boolean;
  log_exists?: boolean;
  manifest?: JobManifest;
};
