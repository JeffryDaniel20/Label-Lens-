import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { api, ApiError } from "@/api/client";
import type { CompleteUploadRequest, FileOut, UploadRequest, UploadResponse } from "@/api/types";

export const filesQueryKey = (versionId: string) =>
  ["product-versions", versionId, "files"] as const;

export function useVersionFiles(versionId: string | undefined) {
  return useQuery({
    queryKey: filesQueryKey(versionId ?? ""),
    queryFn: () => api.get<FileOut[]>(`/v1/product-versions/${versionId}/files`),
    enabled: Boolean(versionId),
  });
}

/** Mirrors `app.storage.router`'s fixed extension allowlist, purely so the
 * UI can reject an obviously-wrong file before spending a round trip - the
 * backend re-validates all of this independently (extension, content-type
 * match, magic bytes, size, AV) and remains the actual authority. */
const EXTENSION_CONTENT_TYPES: Record<string, string> = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  webp: "image/webp",
  heic: "image/heic",
  tif: "image/tiff",
  tiff: "image/tiff",
  pdf: "application/pdf",
};

export function inferContentType(filename: string): string | null {
  const ext = filename.split(".").pop()?.toLowerCase();
  if (!ext) return null;
  return EXTENSION_CONTENT_TYPES[ext] ?? null;
}

export type UploadTaskStatus = "uploading" | "finalizing" | "done" | "error";

export interface UploadTask {
  id: string;
  filename: string;
  progress: number;
  status: UploadTaskStatus;
  error?: string;
  file?: FileOut;
}

function putWithProgress(
  url: string,
  file: File,
  contentType: string,
  onProgress: (pct: number) => void,
) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`Upload to storage failed (status ${xhr.status}).`));
      }
    };
    xhr.onerror = () => reject(new Error("Upload to storage failed (network error)."));
    xhr.send(file);
  });
}

/** Drives the full three-step presigned-upload flow the backend expects
 * (`POST .../uploads` -> raw `PUT` to object storage -> `POST .../files`),
 * per file, tracking per-file progress/status for the drag-drop UI. See
 * `app.storage.router` / `app.ingestion.router` for what each step does. */
export function useFileUploads(versionId: string | undefined) {
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const queryClient = useQueryClient();
  const nextId = useRef(0);

  const updateTask = useCallback((id: string, patch: Partial<UploadTask>) => {
    setTasks((current) => current.map((task) => (task.id === id ? { ...task, ...patch } : task)));
  }, []);

  const uploadOne = useCallback(
    async (id: string, file: File) => {
      const contentType = inferContentType(file.name) ?? file.type;
      try {
        const ticket = await api.post<UploadResponse>(`/v1/product-versions/${versionId}/uploads`, {
          filename: file.name,
          content_type: contentType,
        } satisfies UploadRequest);
        await putWithProgress(ticket.upload_url, file, contentType, (pct) =>
          updateTask(id, { progress: pct }),
        );
        updateTask(id, { status: "finalizing", progress: 100 });
        const completed = await api.post<FileOut>(`/v1/product-versions/${versionId}/files`, {
          key: ticket.key,
          filename: file.name,
        } satisfies CompleteUploadRequest);
        updateTask(id, { status: "done", file: completed });
        await queryClient.invalidateQueries({ queryKey: filesQueryKey(versionId ?? "") });
      } catch (error) {
        // `ApiError.message` is only ever the problem+json `title` (a
        // generic "Request validation failed" for this kind of rejection);
        // the actually useful, specific text - e.g. the disallowed-type or
        // spoofed-upload reason from `app.storage.router`/`app.ingestion.
        // service` - lives in the field-level `errors[]` array instead.
        const message =
          error instanceof ApiError
            ? (error.errors[0]?.message ?? error.message)
            : "Upload failed.";
        updateTask(id, { status: "error", error: message });
      }
    },
    [versionId, updateTask, queryClient],
  );

  const uploadFiles = useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files);
      const newTasks: UploadTask[] = list.map((file) => ({
        id: `upload-${nextId.current++}`,
        filename: file.name,
        progress: 0,
        status: "uploading",
      }));
      setTasks((current) => [...current, ...newTasks]);
      newTasks.forEach((task, index) => {
        const file = list[index];
        if (file) void uploadOne(task.id, file);
      });
    },
    [uploadOne],
  );

  const dismissTask = useCallback((id: string) => {
    setTasks((current) => current.filter((task) => task.id !== id));
  }, []);

  return { tasks, uploadFiles, dismissTask };
}
