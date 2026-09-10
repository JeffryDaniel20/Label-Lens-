import { useQueries, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { DownloadResponse, FilePageOut } from "@/api/types";
import { useVersionFiles } from "@/features/catalog/uploads";

export const filePagesQueryKey = (fileId: string) => ["files", fileId, "pages"] as const;
export const downloadUrlQueryKey = (key: string) => ["files", "download-url", key] as const;

/** One page across every file in a version, with its owning file id
 * attached - the label viewer's "page tabs" are these, in a stable order
 * (file upload order, then page number within each file). */
export interface VersionPage extends FilePageOut {
  fileId: string;
}

/** Every viewable page across every `ready` file in a product version - the
 * composition `GET /product-versions/{id}/files` then, per file, `GET
 * /files/{id}/pages` (no single backend endpoint returns this flattened, so
 * the frontend composes it; both calls are already used elsewhere in this
 * codebase, nothing new on the backend was needed for this part). */
export function useVersionPages(versionId: string | undefined) {
  const filesQuery = useVersionFiles(versionId);
  const readyFiles = (filesQuery.data ?? []).filter((file) => file.status === "ready");

  const pageQueries = useQueries({
    queries: readyFiles.map((file) => ({
      queryKey: filePagesQueryKey(file.id),
      queryFn: () => api.get<FilePageOut[]>(`/v1/files/${file.id}/pages`),
      enabled: Boolean(versionId),
    })),
  });

  const isPending = filesQuery.isPending || pageQueries.some((q) => q.isPending);
  const isError = filesQuery.isError || pageQueries.some((q) => q.isError);

  const pages: VersionPage[] = readyFiles.flatMap((file, index) => {
    const result = pageQueries[index]?.data ?? [];
    return result.map((page) => ({ ...page, fileId: file.id }));
  });

  return { pages, isPending, isError };
}

/** A real, signed, time-limited URL for a rendered page's image bytes
 * (`app.storage.service.request_download` under the hood, same presigning
 * path uploads already use) - never cached across page reloads since it
 * genuinely expires (`expires_in`), but caching it for the component's own
 * lifetime avoids re-requesting one on every re-render. */
export function useDownloadUrl(key: string | undefined) {
  return useQuery({
    queryKey: downloadUrlQueryKey(key ?? ""),
    queryFn: () =>
      api.get<DownloadResponse>(`/v1/files/download-url?key=${encodeURIComponent(key ?? "")}`),
    enabled: Boolean(key),
    staleTime: 60_000,
  });
}
