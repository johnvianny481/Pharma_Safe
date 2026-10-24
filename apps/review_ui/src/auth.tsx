import { User, UserManager } from "oidc-client-ts";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

const CALLBACK_PATH = "/callback";

/** OIDC authorization code + PKCE against the PharmaSafe IdP (Keycloak locally). */
export function createUserManager(): UserManager {
  const origin = window.location.origin;
  return new UserManager({
    authority: import.meta.env.VITE_OIDC_AUTHORITY ?? "http://localhost:8180/realms/pharmasafe",
    client_id: import.meta.env.VITE_OIDC_CLIENT_ID ?? "pharmasafe-ui",
    redirect_uri: `${origin}${CALLBACK_PATH}`,
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
}

export const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ manager, children }: { manager: UserManager; children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        if (window.location.pathname === CALLBACK_PATH) {
          const signedIn = await manager.signinRedirectCallback();
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
    }),
    [manager, user, loading, error],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}
