import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FindingOut } from "@/api/types";
import { FindingsPanel } from "@/components/FindingsPanel";
import { ToastProvider } from "@/lib/toast";

const {
  useFindingEvidenceMock,
  useDecideFindingMock,
  useCorrectFieldMock,
  mutateDecideMock,
  mutateCorrectMock,
} = vi.hoisted(() => ({
  useFindingEvidenceMock: vi.fn(),
  useDecideFindingMock: vi.fn(),
  useCorrectFieldMock: vi.fn(),
  mutateDecideMock: vi.fn(),
  mutateCorrectMock: vi.fn(),
}));
vi.mock("@/features/evidence/evidence", () => ({ useFindingEvidence: useFindingEvidenceMock }));
vi.mock("@/features/review/review", () => ({
  useDecideFinding: useDecideFindingMock,
  useCorrectField: useCorrectFieldMock,
}));

function finding(overrides: Partial<FindingOut>): FindingOut {
  return {
    id: "f1",
    analysis_id: "a1",
    rule_key: "IN-TEST-RULE",
    rule_version: 1,
    status: "fail",
    severity: "major",
    message: "Something is wrong.",
    details: {},
    confidence: 0.9,
    evidence_refs: [],
    rule_title: "A real rule title",
    rule_citation: "Regulation 5(9): a real citation.",
    ...overrides,
  } as FindingOut;
}

function renderPanel(findings: FindingOut[], canDecide = true) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <FindingsPanel
            productId="p1"
            versionId="v1"
            analysisId="a1"
            findings={findings}
            canDecide={canDecide}
            selectedFindingId={undefined}
            onSelectFinding={vi.fn()}
          />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("FindingsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useFindingEvidenceMock.mockReturnValue({ data: undefined, isPending: false });
    useDecideFindingMock.mockReturnValue({ mutate: mutateDecideMock, isPending: false });
    useCorrectFieldMock.mockReturnValue({ mutate: mutateCorrectMock, isPending: false });
  });

  it("shows the empty state when there are no findings", () => {
    renderPanel([]);
    expect(screen.getByText(/no compliance findings/i)).toBeInTheDocument();
  });

  it("renders each finding's real rule title and citation, grouped by severity", () => {
    renderPanel([
      finding({
        id: "f1",
        severity: "critical",
        rule_title: "Critical rule",
        rule_citation: "Regulation 5(8): the critical citation.",
      }),
      finding({
        id: "f2",
        severity: "minor",
        rule_title: "Minor rule",
        rule_citation: "Regulation 5(9): the minor citation.",
      }),
    ]);
    expect(screen.getByText(/critical \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/minor \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText("Critical rule")).toBeInTheDocument();
    expect(screen.getByText("Minor rule")).toBeInTheDocument();
    expect(screen.getByText("Regulation 5(8): the critical citation.")).toBeInTheDocument();
    expect(screen.getByText("Regulation 5(9): the minor citation.")).toBeInTheDocument();
  });

  it("filters findings by status", async () => {
    const user = userEvent.setup();
    renderPanel([
      finding({ id: "f1", status: "fail", rule_title: "Fail rule" }),
      finding({ id: "f2", status: "pass", rule_title: "Pass rule" }),
    ]);
    expect(screen.getByText("Fail rule")).toBeInTheDocument();
    expect(screen.getByText("Pass rule")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "fail" }));

    expect(screen.getByText("Fail rule")).toBeInTheDocument();
    expect(screen.queryByText("Pass rule")).not.toBeInTheDocument();
  });

  it("hides reviewer actions when the viewer cannot decide", () => {
    renderPanel([finding({})], false);
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("confirming a finding calls the decide mutation with that finding's id", async () => {
    const user = userEvent.setup();
    renderPanel([finding({ id: "f1" })]);

    await user.click(screen.getByRole("button", { name: "Confirm" }));

    expect(mutateDecideMock).toHaveBeenCalledWith(
      { findingId: "f1", action: "confirm", reason: undefined },
      expect.anything(),
    );
  });

  it("blocks an override with a too-short reason before ever calling the mutation", async () => {
    const user = userEvent.setup();
    renderPanel([finding({ id: "f1" })]);

    await user.click(screen.getByRole("button", { name: "Override" }));
    await user.type(screen.getByLabelText(/reason/i), "too short");
    await user.click(screen.getByRole("button", { name: "Submit override" }));

    expect(mutateDecideMock).not.toHaveBeenCalled();
  });

  it("submits an override with a real reason", async () => {
    const user = userEvent.setup();
    renderPanel([finding({ id: "f1" })]);

    await user.click(screen.getByRole("button", { name: "Override" }));
    await user.type(
      screen.getByLabelText(/reason/i),
      "The label is actually compliant despite this flag.",
    );
    await user.click(screen.getByRole("button", { name: "Submit override" }));

    expect(mutateDecideMock).toHaveBeenCalledWith(
      {
        findingId: "f1",
        action: "override",
        reason: "The label is actually compliant despite this flag.",
      },
      expect.anything(),
    );
  });

  it("escalating a finding calls the decide mutation with the escalate action", async () => {
    const user = userEvent.setup();
    renderPanel([finding({ id: "f1" })]);

    await user.click(screen.getByRole("button", { name: "Escalate" }));

    expect(mutateDecideMock).toHaveBeenCalledWith(
      { findingId: "f1", action: "escalate", reason: undefined },
      expect.anything(),
    );
  });

  it("only offers Fix field for a finding that has real evidence", () => {
    renderPanel([
      finding({ id: "f1", evidence_refs: [] }),
      finding({
        id: "f2",
        evidence_refs: [{ extracted_field_id: "ef1", evidence_span_id: "es1" }],
      }),
    ]);
    expect(screen.getAllByRole("button", { name: "Fix field" })).toHaveLength(1);
  });

  it("submits a field correction using the real field path from the finding's own evidence", async () => {
    useFindingEvidenceMock.mockReturnValue({
      data: [
        {
          extracted_field_id: "ef1",
          field_path: "dates.batch_number",
          evidence_span_id: "es1",
          file_page_id: "page1",
          page_no: 1,
          bbox: [0, 0, 1, 1],
          text_snippet: "B12345",
          source: "ocr",
          page_image_url: "https://example.com/img.png",
          page_image_expires_in: 300,
        },
      ],
      isPending: false,
    });
    const user = userEvent.setup();
    renderPanel([
      finding({
        id: "f1",
        evidence_refs: [{ extracted_field_id: "ef1", evidence_span_id: "es1" }],
      }),
    ]);

    await user.click(screen.getByRole("button", { name: "Fix field" }));
    await waitFor(() => expect(screen.getByLabelText(/corrected value/i)).toBeInTheDocument());
    await user.type(screen.getByLabelText(/corrected value/i), "B99999");
    await user.click(screen.getByRole("button", { name: "Submit correction" }));

    expect(mutateCorrectMock).toHaveBeenCalledWith(
      { field_path: "dates.batch_number", corrected_value: "B99999" },
      expect.anything(),
    );
  });

  it("supports keyboard-only operation: arrow keys move focus, C confirms the focused finding", async () => {
    const user = userEvent.setup();
    renderPanel([finding({ id: "f1" }), finding({ id: "f2", rule_title: "Second rule" })]);

    const list = screen.getByRole("list", { name: /compliance findings/i });
    list.focus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("c");

    expect(mutateDecideMock).toHaveBeenCalledWith(
      { findingId: "f1", action: "confirm", reason: undefined },
      expect.anything(),
    );
  });
});
