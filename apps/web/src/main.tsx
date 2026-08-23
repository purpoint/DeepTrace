import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./api/client";
import "./index.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // Retrying a 404 asks the same question until the user gives up. Only
      // failures that could plausibly resolve themselves are worth repeating.
      retry: (failureCount, error) =>
        error instanceof ApiError && error.isRetryable && failureCount < 2,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
