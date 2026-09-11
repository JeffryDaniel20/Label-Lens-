/** Convenience aliases over the generated OpenAPI schema
 * (`npm run generate:api-types`, backed by `app.identity.schemas` etc. on
 * the backend) - so features import a plain name instead of the verbose
 * `components["schemas"]["X"]` path everywhere. */
import type { components } from "./schema";

export type MeResponse = components["schemas"]["MeResponse"];
export type LoginRequest = components["schemas"]["LoginRequest"];
export type LoginResponse = components["schemas"]["LoginResponse"];
export type SignupRequest = components["schemas"]["SignupRequest"];

export type ProductOut = components["schemas"]["ProductOut"];
export type ProductCreateRequest = components["schemas"]["ProductCreateRequest"];
export type ProductUpdateRequest = components["schemas"]["ProductUpdateRequest"];
export type ProductVersionOut = components["schemas"]["ProductVersionOut"];
export type ProductVersionStatus = components["schemas"]["ProductVersionStatus"];
export type VersionCreateRequest = components["schemas"]["VersionCreateRequest"];
export type VersionUpdateRequest = components["schemas"]["VersionUpdateRequest"];
export type UploadRequest = components["schemas"]["UploadRequest"];
export type UploadResponse = components["schemas"]["UploadResponse"];
export type DownloadResponse = components["schemas"]["DownloadResponse"];
export type CompleteUploadRequest = components["schemas"]["CompleteUploadRequest"];
export type FileOut = components["schemas"]["FileOut"];
export type FileStatus = components["schemas"]["FileStatus"];
export type AvStatus = components["schemas"]["AvStatus"];
export type FilePageOut = components["schemas"]["FilePageOut"];
export type EvidenceDetailOut = components["schemas"]["EvidenceDetailOut"];

export type AnalysisOut = components["schemas"]["AnalysisOut"];
export type AnalysisState = components["schemas"]["AnalysisState"];
export type ConfidenceTier = components["schemas"]["ConfidenceTier"];
export type FindingOut = components["schemas"]["FindingOut"];
export type FindingStatus = components["schemas"]["FindingStatus"];
export type Severity = components["schemas"]["Severity"];
export type EvidenceRefOut = components["schemas"]["EvidenceRefOut"];
export type ReportOut = components["schemas"]["ReportOut"];

export type DecisionAction = components["schemas"]["DecisionAction"];
export type FindingDecisionOut = components["schemas"]["FindingDecisionOut"];
export type FindingDecisionRequest = components["schemas"]["FindingDecisionRequest"];
export type FieldCorrectionRequest = components["schemas"]["FieldCorrectionRequest"];
export type FieldCorrectionResponse = components["schemas"]["FieldCorrectionResponse"];
export type ReviewQueueEntryOut = components["schemas"]["ReviewQueueEntryOut"];
export type AssignReviewerRequest = components["schemas"]["AssignReviewerRequest"];
export type SignoffOut = components["schemas"]["SignoffOut"];
export type FieldDiffOut = components["schemas"]["FieldDiffOut"];
export type FindingDiffOut = components["schemas"]["FindingDiffOut"];
export type VersionComparisonOut = components["schemas"]["VersionComparisonOut"];

/** `GET /v1/analyses/{id}/events`'s JSON-history shape - the backend route
 * declares `response_model=None` (its real return type is a union with
 * `StreamingResponse`, which FastAPI/OpenAPI can't express as a schema), so
 * `schema.ts` types this endpoint's response as `unknown`. Hand-declared
 * here from `app.analysis.router.AnalysisEventOut` directly; keep in sync
 * with that class if it ever changes. */
export interface AnalysisEventOut {
  sequence: number;
  from_state: AnalysisState | null;
  to_state: AnalysisState;
  occurred_at: string;
  reason: string | null;
}

/** The wire payload of one `event: analysis.transition` SSE message
 * (`app.analysis.sse.format_sse_event`) - the same transition data as
 * `AnalysisEventOut` plus a `percentage`, and NOT the same shape as one
 * element of the JSON-history array above (no generated schema exists for
 * either). Keep in sync with `app.analysis.sse` if the wire format changes. */
export interface AnalysisSseEvent {
  sequence: number;
  from_state: AnalysisState | null;
  to_state: AnalysisState;
  occurred_at: string;
  reason: string | null;
  percentage: number;
}

/** `ReportOut.snapshot` is `dict[str, object]` server-side - a self-
 * contained JSON blob (IMPLEMENTATION.md §19), not a typed schema. This is
 * only the slice this dashboard actually reads (`appendix.extracted_fields`, the
 * real per-field extraction results `app.reports.service.build_snapshot`
 * assembles from `ExtractedField` rows) - treat any other key as unknown. */
export interface ReportSnapshot {
  verdict_summary?: { overall: string };
  appendix?: {
    extracted_fields?: Array<{
      field_path: string;
      value_raw: unknown;
      value_norm: unknown;
      confidence: number;
      verified: boolean;
    }>;
    ocr_text_by_page?: Array<{ file_page_id: string; page_no: number; text: string }>;
  };
}
