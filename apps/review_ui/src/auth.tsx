import { User, UserManager } from "oidc-client-ts";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

const CALLBACK_PATH = "/callback";
export const POPUP_CALLBACK_PATH = "/popup-callback";

/** OIDC authorization code + PKCE against the PharmaSafe IdP (Keycloak locally). */
export function createUserManager(): UserManager {
  const origin = window.location.origin;
  return new UserManager({
    authority: import.meta.env.VITE_OIDC_AUTHORITY ?? "http://localhost:8180/realms/pharmasafe",
    client_id: import.meta.env.VITE_OIDC_CLIENT_ID ?? "pharmasafe-ui",
    redirect_uri: `${origin}${CALLBACK_PATH}`,
    popup_redirect_uri: `${origin}${POPUP_CALLBACK_PATH}`,
    post_logout_redirect_uri: origin,
    response_type: "code",
    scope: "openid",
  });
}

export interface Auth {
  user: User | null;
  loading: boolean;
  error: string | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  /**
   * E-signature re-authentication (21 CFR 11.200): the person enters their credentials
   * again in a popup (`prompt=login`). Returns the fresh access token that the API
   * checks as the signature's re-authentication. Each token can sign once.
   */
  reauthenticate: () => Promise<string>;
}

export const AuthContext = createContext<Auth | null>(null);

// An authorization code can be exchanged once. React may run effects twice (StrictMode
// in development), so the callback is processed once per page load and shared.
const callbacks = new WeakMap<UserManager, Promise<User>>();
const popupHandled = new WeakSet<UserManager>();

function redirectCallbackOnce(manager: UserManager): Promise<User> {
  let pending = callbacks.get(manager);
  if (!pending) {
    pending = manager.signinRedirectCallback();
    callbacks.set(manager, pending);
  }
  return pending;
}

export function AuthProvider({ manager, children }: { manager: UserManager; children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        if (window.location.pathname === CALLBACK_PATH) {
          const signedIn = await redirectCallbackOnce(manager);
          window.history.replaceState({}, "", "/");
          return signedIn;
        }
        const existing = await manager.getUser();
        return existing && !existing.expired ? existing : null;
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
        return null;
      }
    };
    void load().then((loaded) => {
      if (cancelled) return;
      setUser(loaded);
      setLoading(false);
    });
    const onLoaded = (u: User) => setUser(u);
    const onUnloaded = () => setUser(null);
    manager.events.addUserLoaded(onLoaded);
    manager.events.addUserUnloaded(onUnloaded);
    return () => {
      cancelled = true;
      manager.events.removeUserLoaded(onLoaded);
      manager.events.removeUserUnloaded(onUnloaded);
    };
  }, [manager]);

  const value = useMemo<Auth>(
    () => ({
      user,
      loading,
      error,
      signIn: () => manager.signinRedirect(),
      signOut: () => manager.signoutRedirect(),
      reauthenticate: async () => {
        const fresh = await manager.signinPopup({ prompt: "login", max_age: 0 });
        return fresh.access_token;
      },
    }),
    [manager, user, loading, error],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Rendered inside the re-authentication popup: hands the result back and closes. */
export function PopupCallback({ manager }: { manager: UserManager }) {
  useEffect(() => {
    // Once per page load, like the redirect callback: the code can be exchanged once.
    if (!popupHandled.has(manager)) {
      popupHandled.add(manager);
      void manager.signinPopupCallback();
    }
  }, [manager]);
  return <p role="status">Completing sign-in…</p>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}

/** The current access token; components under the signed-in shell always have one. */
export function useToken(): string {
  const { user } = useAuth();
  if (!user) throw new Error("useToken needs a signed-in user");
  return user.access_token;
}
