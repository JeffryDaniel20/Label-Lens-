import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useSession } from "@/features/auth/session";
import { useProduct } from "@/features/catalog/products";
import { useCreateVersion, useProductVersions } from "@/features/catalog/versions";
import type { ProductVersionStatus } from "@/api/types";
import { useToast } from "@/lib/toast";

const STATUS_STYLES: Record<ProductVersionStatus, string> = {
  draft: "bg-amber-100 text-amber-800",
  locked: "bg-blue-100 text-blue-800",
  superseded: "bg-slate-100 text-slate-600",
};

export function ProductDetailPage() {
  const { productId } = useParams<{ productId: string }>();
  const { data: product, isPending, error } = useProduct(productId);
  const { data: versions } = useProductVersions(productId);
  const { data: session } = useSession();
  const canManage = new Set(session?.capabilities ?? []).has("product:manage");
  const [showCreate, setShowCreate] = useState(false);

  if (isPending) return <p className="text-slate-600">Loading product…</p>;
  if (error || !product) return <p className="text-red-700">Could not load this product.</p>;

  return (
    <div>
      <Link to="/products" className="text-sm text-slate-500 hover:text-slate-700">
        &larr; Products
      </Link>
      <h1 className="mt-2 text-2xl font-semibold text-slate-900">{product.name}</h1>
      <p className="mt-1 text-slate-600">
        SKU {product.internal_sku}
        {product.category_hint ? ` · ${product.category_hint}` : ""}
      </p>

      <div className="mt-6 flex items-center justify-between">
        <h2 className="text-lg font-medium text-slate-900">Label versions</h2>
        {canManage && (
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            {showCreate ? "Cancel" : "New version"}
          </button>
        )}
      </div>

      {showCreate && (
        <CreateVersionForm productId={product.id} onDone={() => setShowCreate(false)} />
      )}

      {versions && versions.length === 0 && (
        <p className="mt-4 text-slate-600">No label versions yet.</p>
      )}

      {versions && versions.length > 0 && (
        <ul className="mt-4 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
          {versions.map((version) => (
            <li key={version.id}>
              <Link
                to={`/products/${product.id}/versions/${version.id}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-slate-50"
              >
                <div>
                  <p className="font-medium text-slate-900">
                    v{version.version_no}
                    {version.label ? ` — ${version.label}` : ""}
                  </p>
                </div>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[version.status]}`}
                >
                  {version.status}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CreateVersionForm({ productId, onDone }: { productId: string; onDone: () => void }) {
  const createVersion = useCreateVersion(productId);
  const { showToast } = useToast();
  const [label, setLabel] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    createVersion.mutate(
      { label },
      {
        onSuccess: () => {
          showToast("Version created.", "success");
          onDone();
        },
        onError: (error) => {
          showToast(
            error instanceof ApiError ? error.message : "Could not create version.",
            "error",
          );
        },
      },
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mt-4 max-w-md rounded-md border border-slate-200 bg-white p-4"
    >
      <label htmlFor="version-label" className="block text-sm font-medium text-slate-700">
        Label (optional)
      </label>
      <input
        id="version-label"
        value={label}
        onChange={(event) => setLabel(event.target.value)}
        placeholder="e.g. Diwali 2026 relaunch"
        className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
      />
      <button
        type="submit"
        disabled={createVersion.isPending}
        className="mt-4 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {createVersion.isPending ? "Creating…" : "Create version"}
      </button>
    </form>
  );
}
