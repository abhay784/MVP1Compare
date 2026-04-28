export type JobState = "queued" | "processing" | "complete" | "failed";

export interface ChangeSummary {
  critical: number;
  significant: number;
  minor: number;
  uncertain: number;
  total_views_compared: number;
}

export interface JobStatus {
  job_id: string;
  status: JobState;
  report_url?: string | null;
  changeset_url?: string | null;
  summary?: ChangeSummary | null;
  error?: string | null;
  stage?: string | null;
  progress_pct?: number | null;
}

export interface CompareAccepted {
  job_id: string;
  status_url: string;
  estimated_seconds: number;
}

export async function submitCompare(input: {
  original: File;
  revised: File;
  part_number: string;
  notes: string;
}): Promise<CompareAccepted> {
  const fd = new FormData();
  fd.append("original", input.original);
  fd.append("revised", input.revised);
  fd.append("part_number", input.part_number);
  fd.append("notes", input.notes);

  const res = await fetch("/compare", { method: "POST", body: fd });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Submit failed (${res.status}): ${detail}`);
  }
  return res.json();
}

export async function fetchJobStatus(jobId: string): Promise<JobStatus> {
  const res = await fetch(`/compare/${jobId}`);
  if (!res.ok) throw new Error(`Status fetch failed (${res.status})`);
  return res.json();
}

export const STAGE_LABELS: Record<string, string> = {
  parsing: "Parsing PDFs",
  segmenting: "Detecting views",
  extracting: "Extracting dimensions",
  storing: "Storing extractions",
  awaiting_match: "Matching views",
  awaiting_compare: "Comparing views",
  awaiting_report: "Generating report",
};

export function stageLabel(stage?: string | null): string {
  if (!stage) return "Queued";
  return STAGE_LABELS[stage] ?? stage;
}
