import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AuthProvider } from "./auth";
import { ThemeProvider } from "./theme";
import { ApiError } from "./api/client";
import "./index.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // Retrying a 404 asks the same question until the user gives up. Only
      // failures that could plausibly resolve themselves are worth repeating.
      // A rejected credential is not retried either: the client already
      // refreshed once and was refused, so asking again spends the account's
      // rate limit to receive the same 401.
      retry: (failureCount, error) =>
        error instanceof ApiError &&
        error.isRetryable &&
        !error.needsSignIn &&
        failureCount < 2,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={client}>
        {/* Inside the query client: signing out clears the cache, and one
            person's research must not still be in memory when the next person
            signs in on this machine. */}
        <AuthProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
