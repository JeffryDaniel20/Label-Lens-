import { Link, useNavigate, useParams } from "react-router-dom";

import { UploadDropzone } from "@/components/UploadDropzone";
import { ApiError } from "@/api/client";
import { useSubmitAnalysis } from "@/features/analysis/analysis";
import { useSession } from "@/features/auth/session";
import { useProduct } from "@/features/catalog/products";
import { useFileUploads, useVersionFiles } from "@/features/catalog/uploads";
import { useProductVersion } from "@/features/catalog/versions";
import { useToast } from "@/lib/toast";
import type { AvStatus, FileStatus } from "@/api/types";

const FILE_STATUS_STYLES: Record<FileStatus, string> = {
  uploading: "bg-amber-100 text-amber-800",
  validating: "bg-amber-100 text-amber-800",
  ready: "bg-emerald-100 text-emerald-800",
  rejected: "bg-red-100 text-red-800",
};

const AV_STATUS_LABELS: Record<AvStatus, string> = {
  pending: "AV scan pending",
  clean: "AV clean",
  infected: "AV: threat detected",
  skipped: "AV scan skipped",
  error: "AV scan error",
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

export function VersionDetailPage() {
  const { productId, versionId } = useParams<{ productId: string; versionId: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const { data: product } = useProduct(productId);
  const { data: version, isPending, error } = useProductVersion(versionId);
  const { data: files } = useVersionFiles(versionId);
  const { data: session } = useSession();
  const capabilities = new Set(session?.capabilities ?? []);
  const canUpload = capabilities.has("file:upload");
  const canRunAnalysis = capabilities.has("analysis:run");
  const { tasks, uploadFiles, dismissTask } = useFileUploads(versionId);
  const submitAnalysis = useSubmitAnalysis(versionId ?? "");

  if (isPending) return <p className="text-slate-600">Loading version…</p>;
  if (error || !version) return <p className="text-red-700">Could not load this label version.</p>;

  const isLocked = version.status === "locked";
  const hasReadyFile = (files ?? []).some((file) => file.status === "ready");

  function handleRunAnalysis() {
    submitAnalysis.mutate(undefined, {
      onSuccess: (analysis) => {
        navigate(`/products/${productId}/versions/${versionId}/analyses/${analysis.id}`);
      },
      onError: (err) => {
        showToast(err instanceof ApiError ? err.message : "Could not start analysis.", "error");
      },
    });
  }

  return (
    <div>
      <Link to={`/products/${productId}`} className="text-sm text-slate-500 hover:text-slate-700">
        &larr; {product?.name ?? "Product"}
      </Link>
      <h1 className="mt-2 text-2xl font-semibold text-slate-900">
        v{version.version_no}
        {version.label ? ` — ${version.label}` : ""}
      </h1>
      <p className="mt-1 text-slate-600">
        Status: <span className="font-medium">{version.status}</span>
        {isLocked &&
          " — locked because an analysis depends on it; create a new version to make changes."}
      </p>

      {canUpload && !isLocked && (
        <div className="mt-6">
          <UploadDropzone onFilesSelected={uploadFiles} />
        </div>
      )}

      {tasks.length > 0 && (
        <ul className="mt-4 space-y-2" aria-label="Upload progress">
          {tasks.map((task) => (
            <li
              key={task.id}
              className="flex items-center justify-between rounded-md border border-slate-200 bg-white px-4 py-2 text-sm"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-slate-900">{task.filename}</p>
                {task.status === "error" ? (
                  <p className="text-red-700">{task.error}</p>
                ) : (
                  <div className="mt-1 h-1.5 w-full rounded-full bg-slate-200">
                    <div
                      className="h-1.5 rounded-full bg-slate-700 transition-all"
                      style={{ width: `${task.status === "done" ? 100 : task.progress}%` }}
                    />
                  </div>
                )}
              </div>
              <span className="ml-3 shrink-0 text-xs text-slate-500">
                {task.status === "uploading" && `${task.progress}%`}
                {task.status === "finalizing" && "Validating…"}
                {task.status === "done" && "Done"}
                {task.status === "error" && (
                  <button
                    type="button"
                    onClick={() => dismissTask(task.id)}
                    className="text-slate-500 hover:text-slate-700"
                  >
                    Dismiss
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2 className="mt-8 text-lg font-medium text-slate-900">Files</h2>
      {files && files.length === 0 && <p className="mt-2 text-slate-600">No files uploaded yet.</p>}
      {files && files.length > 0 && (
        <ul
          aria-label="Uploaded files"
          className="mt-4 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white"
        >
          {files.map((file) => (
            <li key={file.id} className="flex items-center justify-between px-4 py-3">
              <div>
                <p className="font-medium text-slate-900">{file.original_filename}</p>
                <p className="text-sm text-slate-500">
                  {file.mime} · {formatBytes(file.bytes)}
                  {file.page_count
                    ? ` · ${file.page_count} page${file.page_count === 1 ? "" : "s"}`
                    : ""}
                  {" · "}
                  {AV_STATUS_LABELS[file.av_status]}
                </p>
                {file.rejection_reason && (
                  <p className="text-sm text-red-700">{file.rejection_reason}</p>
                )}
              </div>
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-medium ${FILE_STATUS_STYLES[file.status]}`}
              >
                {file.status}
              </span>
            </li>
          ))}
        </ul>
      )}

      {canRunAnalysis && (
        <div className="mt-8 rounded-md border border-slate-200 bg-white p-4">
          <h2 className="text-lg font-medium text-slate-900">Analysis</h2>
          <p className="mt-1 text-sm text-slate-600">
            {hasReadyFile
              ? "Start compliance analysis on this label version's uploaded files."
              : "Upload at least one file before starting an analysis."}
          </p>
          <button
            type="button"
            onClick={handleRunAnalysis}
            disabled={!hasReadyFile || submitAnalysis.isPending}
            className="mt-3 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {submitAnalysis.isPending ? "Starting…" : "Run analysis"}
          </button>
        </div>
      )}
    </div>
  );
}
