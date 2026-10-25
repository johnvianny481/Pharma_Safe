import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { AuthProvider, createUserManager, POPUP_CALLBACK_PATH, PopupCallback } from "./auth";
import "./styles.css";

const queryClient = new QueryClient();
const userManager = createUserManager();

const container = document.getElementById("root");
if (!container) throw new Error("Root element #root not found");

createRoot(container).render(
  <StrictMode>
    {window.location.pathname === POPUP_CALLBACK_PATH ? (
      // The e-signature re-authentication popup: hand the result back to the opener.
      <PopupCallback manager={userManager} />
    ) : (
      <AuthProvider manager={userManager}>
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </AuthProvider>
    )}
  </StrictMode>,
);
