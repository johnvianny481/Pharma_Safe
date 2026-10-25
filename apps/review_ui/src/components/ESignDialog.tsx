/**
 * The e-signature dialog (21 CFR Part 11 §11.50, §11.200).
 *
 * It shows exactly what is being signed, with which meaning, and asks for a reason.
 * Signing re-authenticates the person (a fresh login in a popup); the fresh token goes
 * to the API, which binds the signature to the case version's content hash. Nothing is
 * signed if the re-authentication is cancelled or fails.
 */
import { useState } from "react";

import { useAuth } from "../auth";
import { Button, ErrorText, ModalDialog, TextField } from "./ui";

export function ESignDialog(props: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  /** E.g. "Reviewed", "Approved", "Duplicate merged". */
  meaning: string;
  /** What is signed, in words: "Case PS-2026-000123, version 1". */
  subject: string;
  /** Sends the signature with the fresh re-authentication token. */
  onSign: (reauthToken: string, reason: string) => Promise<unknown>;
  reasonRequired?: boolean;
}) {
  const { reauthenticate, user } = useAuth();
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function sign() {
    setBusy(true);
    setError(null);
    try {
      const token = await reauthenticate();
      await props.onSign(token, reason.trim());
      setReason("");
      props.onOpenChange(false);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const name = user?.profile.name ?? user?.profile.preferred_username ?? "you";
  return (
    <ModalDialog isOpen={props.isOpen} onOpenChange={props.onOpenChange} title="Electronic signature">
      <p>
        You are signing <strong>{props.subject}</strong> with the meaning{" "}
        <strong>“{props.meaning}”</strong>.
      </p>
      <p className="hint">
        Signing as {name}. You will be asked to sign in again; your signature is permanently
        linked to this exact version of the case.
      </p>
      <TextField
        label={props.reasonRequired ? "Reason" : "Reason (optional)"}
        value={reason}
        onChange={setReason}
        multiline
        isRequired={props.reasonRequired}
      />
      <ErrorText error={error} />
      <div className="actions">
        <Button variant="quiet" onPress={() => props.onOpenChange(false)}>
          Cancel
        </Button>
        <Button
          variant="primary"
          onPress={() => void sign()}
          isDisabled={busy || (props.reasonRequired && reason.trim() === "")}
        >
          {busy ? "Signing…" : `Sign: ${props.meaning}`}
        </Button>
      </div>
    </ModalDialog>
  );
} // prettier-ignore
