import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AnalysisDashboardPage } from "@/routes/AnalysisDashboardPage";
import { DashboardPage } from "@/routes/DashboardPage";
import { LoginPage } from "@/routes/LoginPage";
import { PlaceholderPage } from "@/routes/PlaceholderPage";
import { ProductDetailPage } from "@/routes/ProductDetailPage";
import { ProductsPage } from "@/routes/ProductsPage";
import { ProtectedRoute } from "@/routes/ProtectedRoute";
import { RootLayout } from "@/routes/RootLayout";
import { VersionDetailPage } from "@/routes/VersionDetailPage";

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: (
      <ProtectedRoute>
        <RootLayout />
      </ProtectedRoute>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "products", element: <ProductsPage /> },
      { path: "products/:productId", element: <ProductDetailPage /> },
      { path: "products/:productId/versions/:versionId", element: <VersionDetailPage /> },
      {
        path: "products/:productId/versions/:versionId/analyses/:analysisId",
        element: <AnalysisDashboardPage />,
      },
      { path: "analyses", element: <PlaceholderPage title="Analyses" /> },
      { path: "reports", element: <PlaceholderPage title="Reports" /> },
      { path: "settings/members", element: <PlaceholderPage title="Members" /> },
      { path: "audit", element: <PlaceholderPage title="Audit log" /> },
    ],
  },
]);

export function App() {
  return <RouterProvider router={router} />;
}
