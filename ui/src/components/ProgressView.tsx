import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJobStatus, JobStatus, stageLabel } from "../api";

interface Props {
  jobId: string;
  onComplete: (job: JobStatus) => void;
  onReset: () => void;
}

const TERMINAL: ReadonlyArray<JobStatus["status"]> = ["complete", "failed"];

export function ProgressView({ jobId, onComplete, onReset }: Props) {
  const query = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => fetchJobStatus(jobId),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s && TERMINAL.includes(s) ? false : 2000;
    },
    refetchIntervalInBackground: true,
  });

  const job = query.data;

  useEffect(() => {
    if (job?.status === "complete") onComplete(job);
  }, [job, onComplete]);

  const pct = job?.progress_pct ?? (job?.status === "queued" ? 0 : 5);
  const label =
    job?.status === "queued"
      ? "Queued"
      : job?.status === "failed"
      ? "Failed"
      : job?.status === "complete"
      ? "Complete"
      : stageLabel(job?.stage);

  const isFailed = job?.status === "failed";

  return (
    <div className="max-w-2xl mx-auto px-6 py-20">
      <div className="text-sm uppercase tracking-wide text-slate-500">
        Job {jobId.slice(0, 8)}
      </div>
      <h1 className="mt-2 text-2xl font-bold text-slate-900">
        {isFailed ? "Comparison failed" : "Comparing drawings…"}
      </h1>

      {!isFailed && (
        <div className="mt-8">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium text-slate-700">{label}</span>
            <span className="text-slate-500">{pct}%</span>
          </div>
          <div className="mt-2 h-3 w-full rounded-full bg-slate-200 overflow-hidden">
            <div
              className="h-full bg-blue-600 transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {isFailed && (
        <div className="mt-6 rounded-md bg-red-50 border border-red-200 p-4 text-sm text-red-800">
          {job?.error ?? "The job failed without a message."}
        </div>
      )}

      {query.isError && (
        <div className="mt-6 rounded-md bg-amber-50 border border-amber-200 p-4 text-sm text-amber-800">
          Polling error: {(query.error as Error).message}
        </div>
      )}

      <div className="mt-10 flex justify-end">
        <button
          onClick={onReset}
          className="text-sm text-slate-600 hover:text-slate-900 underline"
        >
          Start new comparison
        </button>
      </div>
    </div>
  );
}
