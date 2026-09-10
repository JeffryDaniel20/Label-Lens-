import { useRef, useState, type DragEvent } from "react";

interface UploadDropzoneProps {
  onFilesSelected: (files: FileList) => void;
  disabled?: boolean;
}

/** Drag/drop + click-to-browse target. Accepts anything the OS picker
 * offers - the backend (`app.storage.router`'s extension allowlist) is the
 * real authority on what's actually accepted, so this stays permissive
 * rather than duplicating that list into a brittle `accept` attribute. */
export function UploadDropzone({ onFilesSelected, disabled }: UploadDropzoneProps) {
  const [isDragActive, setIsDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragActive(false);
    if (disabled) return;
    if (event.dataTransfer.files.length > 0) {
      onFilesSelected(event.dataTransfer.files);
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label="Upload label files"
      onClick={() => inputRef.current?.click()}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
      }}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setIsDragActive(true);
      }}
      onDragLeave={() => setIsDragActive(false)}
      onDrop={handleDrop}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed px-6 py-10 text-center transition-colors ${
        disabled
          ? "cursor-not-allowed border-slate-200 bg-slate-50 text-slate-400"
          : isDragActive
            ? "border-slate-500 bg-slate-100 text-slate-700"
            : "border-slate-300 text-slate-600 hover:border-slate-400"
      }`}
    >
      <p className="text-sm font-medium">Drag and drop label files here, or click to browse</p>
      <p className="mt-1 text-xs text-slate-500">JPEG, PNG, WebP, HEIC, TIFF or PDF</p>
      <input
        ref={inputRef}
        type="file"
        multiple
        disabled={disabled}
        className="hidden"
        onChange={(event) => {
          if (event.target.files && event.target.files.length > 0) {
            onFilesSelected(event.target.files);
          }
          event.target.value = "";
        }}
      />
    </div>
  );
}
