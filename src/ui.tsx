import { addEventListener, removeEventListener } from "@decky/api";
import { DialogButton, DialogControlsSection, DialogControlsSectionHeader, ProgressBarWithInfo } from "@decky/ui";
import { ReactNode, useEffect, useState } from "react";
import { formatBytes, getJob, Job } from "./backend";

export function useJob() {
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    getJob().then(setJob);
    const l = addEventListener<[Job]>("job", (j) => setJob(j));
    return () => {
      removeEventListener("job", l);
    };
  }, []);
  return job;
}

export function Card(props: { title: string; body: string; onClick?: () => void; warn?: string; disabled?: boolean }) {
  return (
    <DialogButton
      style={{ textAlign: "left", padding: "12px 16px", marginBottom: "8px" }}
      disabled={props.disabled}
      onClick={props.onClick}
    >
      <div style={{ fontWeight: "bold" }}>{props.title}</div>
      <div style={{ fontSize: "13px", opacity: 0.75, marginTop: "2px" }}>{props.body}</div>
      {props.warn && <div style={{ fontSize: "13px", color: "#ffb24a", marginTop: "4px" }}>{props.warn}</div>}
    </DialogButton>
  );
}

export function Section(props: { title: string; children: ReactNode }) {
  return (
    <DialogControlsSection>
      <DialogControlsSectionHeader>{props.title}</DialogControlsSectionHeader>
      {props.children}
    </DialogControlsSection>
  );
}

export function Text(props: { children: ReactNode }) {
  return <div style={{ fontSize: "14px", lineHeight: "1.5", marginBottom: "12px" }}>{props.children}</div>;
}

export function Progress(props: { job: Job | null; onCancel?: () => void }) {
  const j = props.job;
  const pct = j && j.total ? Math.min(100, (j.done / j.total) * 100) : undefined;
  return (
    <>
      <ProgressBarWithInfo
        nProgress={pct}
        indeterminate={pct === undefined}
        sOperationText={j?.message || j?.phase || "Starting"}
        description={j && j.total ? `${formatBytes(j.done)} of ${formatBytes(j.total)}` : ""}
      />
      {props.onCancel && (
        <DialogButton style={{ marginTop: "12px", width: "160px" }} onClick={props.onCancel}>
          Cancel
        </DialogButton>
      )}
    </>
  );
}

