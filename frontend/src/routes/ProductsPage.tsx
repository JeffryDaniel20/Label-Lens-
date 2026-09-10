import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useCreateProduct, useProducts } from "@/features/catalog/products";
import { useSession } from "@/features/auth/session";
import { useToast } from "@/lib/toast";

export function ProductsPage() {
  const { data: products, isPending, error } = useProducts();
  const { data: session } = useSession();
  const canManage = new Set(session?.capabilities ?? []).has("product:manage");
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900">Products</h1>
        {canManage && (
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            {showCreate ? "Cancel" : "New product"}
          </button>
        )}
      </div>

      {showCreate && <CreateProductForm onDone={() => setShowCreate(false)} />}

      {isPending && <p className="mt-6 text-slate-600">Loading products…</p>}
      {error && <p className="mt-6 text-red-700">Could not load products.</p>}

      {products && products.length === 0 && (
        <p className="mt-6 text-slate-600">No products yet. Create one to get started.</p>
      )}

      {products && products.length > 0 && (
        <ul className="mt-6 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
          {products.map((product) => (
            <li key={product.id}>
              <Link
                to={`/products/${product.id}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-slate-50"
              >
                <div>
                  <p className="font-medium text-slate-900">{product.name}</p>
                  <p className="text-sm text-slate-500">SKU {product.internal_sku}</p>
                </div>
                {product.market_codes.length > 0 && (
                  <span className="text-sm text-slate-500">{product.market_codes.join(", ")}</span>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CreateProductForm({ onDone }: { onDone: () => void }) {
  const createProduct = useCreateProduct();
  const { showToast } = useToast();
  const [name, setName] = useState("");
  const [internalSku, setInternalSku] = useState("");
  const [marketCodes, setMarketCodes] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    createProduct.mutate(
      {
        name,
        internal_sku: internalSku,
        market_codes: marketCodes
          .split(",")
          .map((code) => code.trim())
          .filter(Boolean),
      },
      {
        onSuccess: () => {
          showToast("Product created.", "success");
          onDone();
        },
        onError: (error) => {
          showToast(
            error instanceof ApiError ? error.message : "Could not create product.",
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
      <div>
        <label htmlFor="product-name" className="block text-sm font-medium text-slate-700">
          Name
        </label>
        <input
          id="product-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
        />
      </div>
      <div className="mt-3">
        <label htmlFor="product-sku" className="block text-sm font-medium text-slate-700">
          Internal SKU
        </label>
        <input
          id="product-sku"
          value={internalSku}
          onChange={(event) => setInternalSku(event.target.value)}
          required
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
        />
      </div>
      <div className="mt-3">
        <label htmlFor="product-markets" className="block text-sm font-medium text-slate-700">
          Market codes (comma separated)
        </label>
        <input
          id="product-markets"
          value={marketCodes}
          onChange={(event) => setMarketCodes(event.target.value)}
          placeholder="IN, US"
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
        />
      </div>
      <button
        type="submit"
        disabled={createProduct.isPending}
        className="mt-4 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {createProduct.isPending ? "Creating…" : "Create product"}
      </button>
    </form>
  );
}
