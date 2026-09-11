import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "@/api/client";
import type { FindingOut, FindingStatus, Severity } from "@/api/types";
import { useFindingEvidence } from "@/features/evidence/evidence";
import { useCorrectField, useDecideFinding } from "@/features/review/review";
import { useToast } from "@/lib/toast";

const SEVERITY_STYLES: Record<Severity, string> = {
  critical: "bg-red-100 text-red-800",
  major: "bg-orange-100 text-orange-800",
  minor: "bg-amber-100 text-amber-800",
  advisory: "bg-slate-100 text-slate-700",
};

const STATUS_STYLES: Record<FindingStatus, string> = {
  pass: "bg-emerald-100 text-emerald-800",
  fail: "bg-red-100 text-red-800",
  insufficient_data: "bg-amber-100 text-amber-800",
  not_applicable: "bg-slate-100 text-slate-700",
};

const SEVERITY_ORDER: Severity[] = ["critical", "major", "minor", "advisory"];
const STATUS_FILTERS: Array<FindingStatus | "all"> = [
  "all",
  "fail",
  "insufficient_data",
  "pass",
  "not_applicable",
];
const OVERRIDE_REASON_MIN_LENGTH = 20;

interface FindingsPanelProps {
  productId: string;
  versionId: string;
  analysisId: string;
  findings: FindingOut[];
  canDecide: boolean;
  selectedFindingId: string | undefined;
  onSelectFinding: (findingId: string | undefined) => void;
}

/** P6-T5: grouped/filterable findings, each with its rule text + citation,
 * and the reviewer actions IMPLEMENTATION.md §12 names - Confirm, Override
 * (mandatory reason), Fix field, Escalate. Fully keyboard-operable (the
 * task's own acceptance criterion): arrow keys move focus between findings,
 * `Enter` opens/views a finding's evidence, `c`/`o`/`e`/`f` trigger the four
 * actions on the focused finding without ever touching a mouse. */
export function FindingsPanel({
  productId,
  versionId,
  analysisId,
  findings,
  canDecide,
  selectedFindingId,
  onSelectFinding,
}: FindingsPanelProps) {
  const [statusFilter, setStatusFilter] = useState<FindingStatus | "all">("all");
  const [focusedId, setFocusedId] = useState<string | undefined>(undefined);
  const [overrideDraftId, setOverrideDraftId] = useState<string | undefined>(undefined);
  const [overrideReason, setOverrideReason] = useState("");
  const [correctDraftId, setCorrectDraftId] = useState<string | undefined>(undefined);
  const [correctedValue, setCorrectedValue] = useState("");
  const itemRefs = useRef(new Map<string, HTMLLIElement>());
  const { showToast } = useToast();
  const navigate = useNavigate();

  const filtered = useMemo(
    () => (statusFilter === "all" ? findings : findings.filter((f) => f.status === statusFilter)),
    [findings, statusFilter],
  );
  const grouped = useMemo(() => {
    const groups = new Map<Severity, FindingOut[]>();
    for (const severity of SEVERITY_ORDER) groups.set(severity, []);
    for (const finding of filtered) groups.get(finding.severity)?.push(finding);
    return groups;
  }, [filtered]);

  const orderedIds = filtered.map((f) => f.id);
  const focused = findings.find((f) => f.id === focusedId);

  const evidenceQuery = useFindingEvidence(correctDraftId ?? focusedId);
  const decideFinding = useDecideFinding();
  const correctField = useCorrectField(analysisId);

  function moveFocus(delta: number) {
    if (orderedIds.length === 0) return;
    const currentIndex = focusedId ? orderedIds.indexOf(focusedId) : -1;
    const nextIndex = (currentIndex + delta + orderedIds.length) % orderedIds.length;
    const nextId = orderedIds[nextIndex];
    if (!nextId) return;
    setFocusedId(nextId);
    // Guarded rather than a plain optional-chained call: jsdom (this
    // project's test environment) doesn't implement `scrollIntoView` at
    // all, so `element?.scrollIntoView()` would still throw - optional
    // chaining only skips a null/undefined *object*, not a missing method
    // on a real one.
    const element = itemRefs.current.get(nextId);
    if (typeof element?.scrollIntoView === "function") {
      element.scrollIntoView({ block: "nearest" });
    }
  }

  function decide(findingId: string, action: "confirm" | "override" | "escalate", reason?: string) {
    decideFinding.mutate(
      { findingId, action, reason },
      {
        onSuccess: () => showToast(`Finding ${action}ed.`, "success"),
        onError: (err) => {
          showToast(err instanceof ApiError ? err.message : `Could not ${action}.`, "error");
        },
      },
    );
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLUListElement>) {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveFocus(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveFocus(-1);
        return;
      default:
    }
    if (!focused) return;
    switch (event.key) {
      case "Enter":
        event.preventDefault();
        if (focused.evidence_refs.length > 0) onSelectFinding(focused.id);
        return;
      case "c":
      case "C":
        if (canDecide) decide(focused.id, "confirm");
        return;
      case "o":
      case "O":
        if (canDecide) setOverrideDraftId(focused.id);
        return;
      case "e":
      case "E":
        if (canDecide) decide(focused.id, "escalate");
        return;
      case "f":
      case "F":
        if (canDecide && focused.evidence_refs.length > 0) setCorrectDraftId(focused.id);
        return;
      default:
    }
  }

  function submitOverride(findingId: string) {
    if (overrideReason.trim().length < OVERRIDE_REASON_MIN_LENGTH) {
      showToast(
        `An override needs a reason of at least ${OVERRIDE_REASON_MIN_LENGTH} characters.`,
        "error",
      );
      return;
    }
    decide(findingId, "override", overrideReason.trim());
    setOverrideDraftId(undefined);
    setOverrideReason("");
  }

  function submitCorrection() {
    const fieldPath = evidenceQuery.data?.[0]?.field_path;
    if (!fieldPath || !correctedValue.trim()) return;
    correctField.mutate(
      { field_path: fieldPath, corrected_value: correctedValue.trim() },
      {
        onSuccess: (response) => {
          showToast("Field corrected - a new analysis was created.", "success");
          setCorrectDraftId(undefined);
          setCorrectedValue("");
          navigate(
            `/products/${productId}/versions/${versionId}/analyses/${response.child_analysis.id}`,
          );
        },
        onError: (err) => {
          showToast(err instanceof ApiError ? err.message : "Could not correct field.", "error");
        },
      },
    );
  }

  if (findings.length === 0) {
    return (
      <p className="mt-2 text-slate-600">No compliance findings are available for this analysis.</p>
    );
  }

  return (
    <div className="mt-4">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by status">
        {STATUS_FILTERS.map((status) => (
          <button
            key={status}
            type="button"
            onClick={() => setStatusFilter(status)}
            aria-pressed={statusFilter === status}
            className={`rounded-full px-2.5 py-1 text-xs font-medium ${
              statusFilter === status
                ? "bg-slate-900 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {status === "all" ? "All" : status.replace("_", " ")}
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Keyboard: ↑/↓ to move, Enter to view evidence, C confirm, O override, E escalate, F fix
        field.
      </p>

      <ul
        className="mt-3 space-y-4"
        onKeyDown={handleKeyDown}
        tabIndex={0}
        aria-label="Compliance findings, grouped by severity"
      >
        {SEVERITY_ORDER.map((severity) => {
          const items = grouped.get(severity) ?? [];
          if (items.length === 0) return null;
          return (
            <li key={severity}>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                {severity} ({items.length})
              </h3>
              <ul className="mt-1.5 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
                {items.map((finding) => {
                  const hasEvidence = finding.evidence_refs.length > 0;
                  const isSelected = finding.id === selectedFindingId;
                  const isFocused = finding.id === focusedId;
                  return (
                    <li
                      key={finding.id}
                      ref={(el) => {
                        if (el) itemRefs.current.set(finding.id, el);
                        else itemRefs.current.delete(finding.id);
                      }}
                      className={isFocused ? "ring-2 ring-inset ring-slate-400" : ""}
                    >
                      <div
                        className={`px-4 py-3 ${isSelected ? "bg-amber-50" : ""}`}
                        onFocus={() => setFocusedId(finding.id)}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <button
                            type="button"
                            disabled={!hasEvidence}
                            onClick={() => {
                              setFocusedId(finding.id);
                              onSelectFinding(finding.id);
                            }}
                            className={`text-left ${hasEvidence ? "cursor-pointer" : "cursor-default"}`}
                          >
                            <p className="font-medium text-slate-900">
                              {finding.rule_title ?? finding.rule_key}
                            </p>
                            {finding.rule_citation && (
                              <p className="mt-0.5 text-xs italic text-slate-500">
                                {finding.rule_citation}
                              </p>
                            )}
                            {finding.message && (
                              <p className="mt-1 text-sm text-slate-600">{finding.message}</p>
                            )}
                            {hasEvidence && (
                              <p className="mt-0.5 text-xs text-slate-500">
                                {isSelected ? "Showing evidence above ↑" : "Click to view evidence"}
                              </p>
                            )}
                          </button>
                          <div className="flex shrink-0 gap-2">
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs font-medium ${SEVERITY_STYLES[finding.severity]}`}
                            >
                              {finding.severity}
                            </span>
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[finding.status]}`}
                            >
                              {finding.status}
                            </span>
                          </div>
                        </div>

                        {canDecide && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => decide(finding.id, "confirm")}
                              className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                            >
                              Confirm
                            </button>
                            <button
                              type="button"
                              onClick={() => setOverrideDraftId(finding.id)}
                              className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                            >
                              Override
                            </button>
                            <button
                              type="button"
                              onClick={() => decide(finding.id, "escalate")}
                              className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                            >
                              Escalate
                            </button>
                            {hasEvidence && (
                              <button
                                type="button"
                                onClick={() => setCorrectDraftId(finding.id)}
                                className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                              >
                                Fix field
                              </button>
                            )}
                          </div>
                        )}

                        {overrideDraftId === finding.id && (
                          <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2">
                            <label
                              htmlFor={`override-reason-${finding.id}`}
                              className="text-xs font-medium text-slate-700"
                            >
                              Reason (min {OVERRIDE_REASON_MIN_LENGTH} characters)
                            </label>
                            <textarea
                              id={`override-reason-${finding.id}`}
                              value={overrideReason}
                              onChange={(e) => setOverrideReason(e.target.value)}
                              className="mt-1 w-full rounded-md border border-slate-300 p-1.5 text-sm"
                              rows={2}
                            />
                            <div className="mt-1.5 flex gap-2">
                              <button
                                type="button"
                                onClick={() => submitOverride(finding.id)}
                                className="rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-white"
                              >
                                Submit override
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  setOverrideDraftId(undefined);
                                  setOverrideReason("");
                                }}
                                className="rounded-md px-2 py-1 text-xs font-medium text-slate-600"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        )}

                        {correctDraftId === finding.id && (
                          <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2">
                            {evidenceQuery.isPending && (
                              <p className="text-xs text-slate-500">Loading field…</p>
                            )}
                            {evidenceQuery.data?.[0] && (
                              <>
                                <label
                                  htmlFor={`correct-value-${finding.id}`}
                                  className="text-xs font-medium text-slate-700"
                                >
                                  Corrected value for {evidenceQuery.data[0].field_path} (currently
                                  "{evidenceQuery.data[0].text_snippet}")
                                </label>
                                <input
                                  id={`correct-value-${finding.id}`}
                                  value={correctedValue}
                                  onChange={(e) => setCorrectedValue(e.target.value)}
                                  className="mt-1 w-full rounded-md border border-slate-300 p-1.5 text-sm"
                                />
                                <div className="mt-1.5 flex gap-2">
                                  <button
                                    type="button"
                                    onClick={submitCorrection}
                                    disabled={correctField.isPending}
                                    className="rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
                                  >
                                    {correctField.isPending
                                      ? "Re-evaluating…"
                                      : "Submit correction"}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => {
                                      setCorrectDraftId(undefined);
                                      setCorrectedValue("");
                                    }}
                                    className="rounded-md px-2 py-1 text-xs font-medium text-slate-600"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
