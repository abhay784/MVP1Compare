import { useRef, useState, DragEvent, ChangeEvent } from "react";

interface Props {
  label: string;
  file: File | null;
  onFile: (file: File | null) => void;
}

export function FileDropSlot({ label, file, onFile }: Props) {
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const accept = (f: File | null) => {
    if (!f) return onFile(null);
    if (f.type !== "application/pdf") {
      alert(`${label}: only PDF files are accepted.`);
      return;
    }
    onFile(f);
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    accept(e.dataTransfer.files?.[0] ?? null);
  };

  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    accept(e.target.files?.[0] ?? null);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
      onClick={() => inputRef.current?.click()}
      className={[
        "flex flex-col items-center justify-center",
        "rounded-lg border-2 border-dashed p-8 cursor-pointer transition",
        dragOver
          ? "border-blue-500 bg-blue-50"
          : file
          ? "border-green-500 bg-green-50"
          : "border-slate-300 bg-white hover:border-slate-400",
      ].join(" ")}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={onChange}
      />
      <div className="text-sm font-medium text-slate-500 uppercase tracking-wide">
        {label}
      </div>
      {file ? (
        <>
          <div className="mt-3 text-slate-900 font-medium break-all text-center">
            {file.name}
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {(file.size / 1024 / 1024).toFixed(2)} MB
          </div>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onFile(null);
            }}
            className="mt-3 text-xs text-slate-500 hover:text-red-600 underline"
          >
            Remove
          </button>
        </>
      ) : (
        <>
          <div className="mt-3 text-slate-600">Drop a PDF here</div>
          <div className="text-xs text-slate-400 mt-1">or click to browse</div>
        </>
      )}
    </div>
  );
}
