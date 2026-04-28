import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { submitCompare } from "../api";
import { FileDropSlot } from "./FileDropSlot";

interface Props {
  onSubmitted: (jobId: string) => void;
}

export function UploadView({ onSubmitted }: Props) {
  const [original, setOriginal] = useState<File | null>(null);
  const [revised, setRevised] = useState<File | null>(null);
  const [partNumber, setPartNumber] = useState("");
  const [notes, setNotes] = useState("");

  const mutation = useMutation({
    mutationFn: submitCompare,
    onSuccess: (data) => onSubmitted(data.job_id),
  });

  const canSubmit = original && revised && !mutation.isPending;

  const handleSubmit = () => {
    if (!original || !revised) return;
    mutation.mutate({
      original,
      revised,
      part_number: partNumber,
      notes,
    });
  };

  return (
    <div className="max-w-4xl mx-auto px-6 py-12">
      <h1 className="text-3xl font-bold text-slate-900">DrawDiff</h1>
      <p className="mt-2 text-slate-600">
        Compare two revisions of an engineering drawing. Upload both PDFs to
        begin.
      </p>

      <div className="mt-8 grid grid-cols-1 md:grid-cols-2 gap-6">
        <FileDropSlot label="Original" file={original} onFile={setOriginal} />
        <FileDropSlot label="Revised" file={revised} onFile={setRevised} />
      </div>

      <div className="mt-6 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label className="block text-sm font-medium text-slate-700">
            Part number <span className="text-slate-400">(optional)</span>
          </label>
          <input
            type="text"
            value={partNumber}
            onChange={(e) => setPartNumber(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
            placeholder="e.g. PN-123456"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-700">
            Notes <span className="text-slate-400">(optional)</span>
          </label>
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
            placeholder="Context for reviewers"
          />
        </div>
      </div>

      {mutation.isError && (
        <div className="mt-6 rounded-md bg-red-50 border border-red-200 p-4 text-sm text-red-800">
          {(mutation.error as Error).message}
        </div>
      )}

      <div className="mt-8 flex justify-end">
        <button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="px-6 py-2.5 rounded-md bg-blue-600 text-white font-medium hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition"
        >
          {mutation.isPending ? "Submitting…" : "Compare"}
        </button>
      </div>
    </div>
  );
}
