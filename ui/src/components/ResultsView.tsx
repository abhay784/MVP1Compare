import { JobStatus } from "../api";

interface Props {
  job: JobStatus;
  onReset: () => void;
}

const BADGES: Array<{
  key: "critical" | "significant" | "minor" | "uncertain";
  label: string;
  classes: string;
}> = [
  {
    key: "critical",
    label: "Critical",
    classes: "bg-red-50 border-red-200 text-red-800",
  },
  {
    key: "significant",
    label: "Significant",
    classes: "bg-orange-50 border-orange-200 text-orange-800",
  },
  {
    key: "minor",
    label: "Minor",
    classes: "bg-yellow-50 border-yellow-200 text-yellow-800",
  },
  {
    key: "uncertain",
    label: "Uncertain",
    classes: "bg-slate-50 border-slate-300 text-slate-700",
  },
];

export function ResultsView({ job, onReset }: Props) {
  const summary = job.summary;

  return (
    <div className="max-w-4xl mx-auto px-6 py-16">
      <div className="text-sm uppercase tracking-wide text-slate-500">
        Job {job.job_id.slice(0, 8)} · Complete
      </div>
      <h1 className="mt-2 text-3xl font-bold text-slate-900">
        Comparison complete
      </h1>
      {summary && (
        <p className="mt-2 text-slate-600">
          Compared {summary.total_views_compared}{" "}
          {summary.total_views_compared === 1 ? "view" : "views"}.
        </p>
      )}

      <div className="mt-8 grid grid-cols-2 md:grid-cols-4 gap-4">
        {BADGES.map((b) => (
          <div
            key={b.key}
            className={`rounded-lg border p-4 ${b.classes}`}
          >
            <div className="text-xs uppercase tracking-wide opacity-80">
              {b.label}
            </div>
            <div className="mt-2 text-3xl font-bold">
              {summary?.[b.key] ?? 0}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-10 flex flex-wrap gap-4">
        {job.report_url && (
          <a
            href={job.report_url}
            target="_blank"
            rel="noreferrer"
            className="px-6 py-2.5 rounded-md bg-blue-600 text-white font-medium hover:bg-blue-700 transition"
          >
            Download Report
          </a>
        )}
        {job.changeset_url && (
          <a
            href={job.changeset_url}
            target="_blank"
            rel="noreferrer"
            className="px-6 py-2.5 rounded-md border border-slate-300 bg-white text-slate-700 font-medium hover:bg-slate-50 transition"
          >
            Download Changeset JSON
          </a>
        )}
        <button
          onClick={onReset}
          className="px-6 py-2.5 rounded-md text-slate-600 hover:text-slate-900 font-medium"
        >
          Start new comparison
        </button>
      </div>
    </div>
  );
}
