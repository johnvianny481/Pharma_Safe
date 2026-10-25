/**
 * Thin, styled wrappers over React Aria Components. React Aria supplies the
 * accessibility (labels, keyboard support, focus management, ARIA roles); these add
 * the app's look and a consistent label/description/error layout.
 */
import type { ReactNode } from "react";
import {
  Button as AriaButton,
  type ButtonProps,
  Dialog,
  FieldError,
  Heading,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Modal,
  ModalOverlay,
  Popover,
  Select,
  SelectValue,
  Text,
  TextArea,
  TextField as AriaTextField,
} from "react-aria-components";

export function Button({
  variant = "secondary",
  className,
  ...props
}: ButtonProps & { variant?: "primary" | "secondary" | "danger" | "quiet" }) {
  return <AriaButton {...props} className={`btn btn-${variant} ${className ?? ""}`} />;
}

export function TextField(props: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  description?: string;
  multiline?: boolean;
  isRequired?: boolean;
  isReadOnly?: boolean;
  type?: string;
  inputMode?: "decimal" | "text" | "email";
  name?: string;
  onBlur?: () => void;
  errorMessage?: string;
}) {
  return (
    <AriaTextField
      className="field"
      value={props.value}
      onChange={props.onChange}
      onBlur={props.onBlur}
      isRequired={props.isRequired}
      isReadOnly={props.isReadOnly}
      isInvalid={Boolean(props.errorMessage)}
      type={props.type}
      inputMode={props.inputMode}
      name={props.name}
    >
      <Label>{props.label}</Label>
      {props.multiline ? <TextArea rows={5} /> : <Input />}
      {props.description && <Text slot="description">{props.description}</Text>}
      <FieldError>{props.errorMessage}</FieldError>
    </AriaTextField>
  );
}

export function SelectField(props: {
  label: string;
  value: string | null;
  options: readonly (readonly [string, string])[];
  onChange: (value: string | null) => void;
  placeholder?: string;
  isDisabled?: boolean;
}) {
  return (
    <Select
      className="field"
      selectedKey={props.value}
      onSelectionChange={(key) => props.onChange(key === null ? null : String(key))}
      placeholder={props.placeholder ?? "Not given"}
      isDisabled={props.isDisabled}
    >
      <Label>{props.label}</Label>
      <AriaButton className="select-button">
        <SelectValue />
        <span aria-hidden="true">▾</span>
      </AriaButton>
      <Popover className="popover">
        <ListBox className="listbox">
          {props.options.map(([value, text]) => (
            <ListBoxItem key={value} id={value} className="option">
              {text}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </Select>
  );
}

export function Badge({ tone, children, label }: {
  tone: "info" | "warn" | "danger" | "ok" | "muted";
  children: ReactNode;
  /** Screen-reader text when the visible text alone is not enough. */
  label?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} aria-label={label}>
      {children}
    </span>
  );
} // prettier-ignore

/** A modal dialog with a heading; focus is trapped and restored by React Aria. */
export function ModalDialog(props: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <ModalOverlay
      className="modal-overlay"
      isOpen={props.isOpen}
      onOpenChange={props.onOpenChange}
      isDismissable
    >
      <Modal className={`modal ${props.wide ? "modal-wide" : ""}`}>
        <Dialog className="dialog">
          <Heading slot="title" className="dialog-title">
            {props.title}
          </Heading>
          {props.children}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p role="alert" className="error">{error instanceof Error ? error.message : String(error)}</p>;
} // prettier-ignore
