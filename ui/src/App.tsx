import { useState } from "react";
import { UploadView } from "./components/UploadView";
import { ProgressView } from "./components/ProgressView";
import { ResultsView } from "./components/ResultsView";
import { JobStatus } from "./api";

type View =
  | { kind: "upload" }
  | { kind: "progress"; jobId: string }
  | { kind: "results"; job: JobStatus };

export default function App() {
  const [view, setView] = useState<View>({ kind: "upload" });

  const reset = () => setView({ kind: "upload" });

  return (
    <div className="min-h-screen">
      {view.kind === "upload" && (
        <UploadView
          onSubmitted={(jobId) => setView({ kind: "progress", jobId })}
        />
      )}
      {view.kind === "progress" && (
        <ProgressView
          jobId={view.jobId}
          onComplete={(job) => setView({ kind: "results", job })}
          onReset={reset}
        />
      )}
      {view.kind === "results" && (
        <ResultsView job={view.job} onReset={reset} />
      )}
    </div>
  );
}
